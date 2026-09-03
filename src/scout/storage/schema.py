"""Clean-bootstrap DDL for scout's SQLite database.

Owns SCHEMA — the single hand-edited source of truth a genuinely empty
database is bootstrapped from in one shot — and LATEST_SCHEMA_VERSION, the
schema version that bootstrap and every migration in migrations.py both
converge on. Dependency-light by design: this module imports nothing
project-local, so it can never be part of an import cycle.
"""

from __future__ import annotations

LATEST_SCHEMA_VERSION = 37

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    messages_scanned INTEGER DEFAULT 0,
    relevant_found INTEGER DEFAULT 0,
    fetch_started_at TEXT,
    safe_watermark_at TEXT,
    status TEXT,
    overflow_count INTEGER DEFAULT 0,
    dossier_revision TEXT,
    environment TEXT NOT NULL DEFAULT 'unknown',
    run_kind TEXT NOT NULL DEFAULT 'unknown'
);

CREATE TABLE IF NOT EXISTS parent_context_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id INTEGER NOT NULL UNIQUE,
    assessor TEXT NOT NULL,
    assessed_at TEXT NOT NULL,
    without_parent_relevance TEXT NOT NULL,
    without_parent_posture TEXT NOT NULL,
    explanation TEXT NOT NULL,
    FOREIGN KEY (evaluation_id) REFERENCES evaluations(id)
);

CREATE TABLE IF NOT EXISTS scan_fetch_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    context TEXT,
    kind TEXT NOT NULL,
    message TEXT,
    http_status INTEGER,
    retry_after TEXT,
    retryable INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (scan_id) REFERENCES scans(id)
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_msg_id TEXT NOT NULL,
    channel_name TEXT,
    channel_id TEXT,
    author_name TEXT,
    author_id TEXT,
    content TEXT,
    url TEXT,
    created_at TEXT,
    scan_id INTEGER,
    parent_lookup_status TEXT NOT NULL DEFAULT 'not_applicable',
    parent_id TEXT,
    parent_author_id TEXT,
    parent_author_name TEXT,
    parent_text TEXT,
    parent_url TEXT,
    UNIQUE(platform, platform_msg_id)
);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    relevant INTEGER NOT NULL,
    score REAL NOT NULL,
    reason TEXT,
    relevant_to TEXT,
    keyword_route_id INTEGER,
    scan_id INTEGER,
    created_at TEXT,
    project_key TEXT,
    posture TEXT,
    -- Deprecated compatibility column: the active terminal-reason contract
    -- is failure_reason (all outcomes, including abstained). Migration 17
    -- already classified historical posture='abstain' rows as abstained;
    -- no rows require abstain_reason and it is not written going forward.
    abstain_reason TEXT,
    surface_status TEXT NOT NULL CHECK(surface_status IN (
        'surfaced', 'low_relevance', 'abstained', 'critic_rejected',
        'gate_blocked', 'not_relevant', 'drafting_failed'
    )),
    failure_reason TEXT,
    dossier_summary_id TEXT,
    dossier_revision TEXT,
    FOREIGN KEY (post_id) REFERENCES posts(id),
    FOREIGN KEY (keyword_route_id) REFERENCES project_keywords(id)
);

CREATE TABLE IF NOT EXISTS draft_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    evaluation_id INTEGER NOT NULL,
    project_key TEXT,
    comment_text TEXT,
    created_at TEXT,
    scan_id INTEGER,
    posture TEXT,
    structured_output TEXT,
    dossier_summary_id TEXT,
    dossier_revision TEXT,
    FOREIGN KEY (post_id) REFERENCES posts(id),
    FOREIGN KEY (evaluation_id) REFERENCES evaluations(id)
);

CREATE TABLE IF NOT EXISTS critiques (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER,
    evaluation_id INTEGER,
    verdict TEXT NOT NULL,
    feedback TEXT,
    created_at TEXT,
    scan_id INTEGER,
    FOREIGN KEY (draft_id) REFERENCES draft_comments(id),
    FOREIGN KEY (evaluation_id) REFERENCES evaluations(id),
    CHECK(draft_id IS NOT NULL OR evaluation_id IS NOT NULL)
);

-- Immutable, ordered lineage of corrected reply text for a draft_comments
-- row (v34) — the reply-pipeline counterpart to the content engine's former
-- draft_revisions table (dropped in v37), using the
-- same version/parent_revision_id shape and the grade_revisions-style
-- append-only UPDATE/DELETE triggers. grades.reply_revision_id below
-- points at the exact revision a grade was recorded against.
CREATE TABLE IF NOT EXISTS reply_draft_revisions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_comment_id   INTEGER NOT NULL,
    version            INTEGER NOT NULL,
    parent_revision_id INTEGER,
    reply_text         TEXT NOT NULL,
    source             TEXT NOT NULL CHECK(
        source IN ('cli', 'web', 'migration') AND length(trim(source)) > 0
    ),
    created_at         TEXT NOT NULL,
    UNIQUE (draft_comment_id, version),
    FOREIGN KEY (draft_comment_id) REFERENCES draft_comments(id),
    FOREIGN KEY (parent_revision_id) REFERENCES reply_draft_revisions(id)
);

CREATE INDEX IF NOT EXISTS reply_draft_revisions_draft_comment_id_idx
    ON reply_draft_revisions(draft_comment_id);

CREATE TRIGGER IF NOT EXISTS reply_draft_revisions_no_update
BEFORE UPDATE ON reply_draft_revisions
BEGIN
    SELECT RAISE(ABORT, 'reply_draft_revisions is immutable');
END;

CREATE TRIGGER IF NOT EXISTS reply_draft_revisions_no_delete
BEFORE DELETE ON reply_draft_revisions
BEGIN
    SELECT RAISE(ABORT, 'reply_draft_revisions is immutable');
END;

-- source and relevance/action enum domains below must track
-- web/grading_schema.json (relevance_judgments, action_judgments) plus the
-- 'migration' source used only by StateManager.save_grade_for_migration.
-- These CHECKs are storage-level invariants only — they do not encode the
-- conditional causal rules (dimension membership, cross-field
-- requirements) that validate_grade enforces at the write boundary.
CREATE TABLE IF NOT EXISTS grades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id INTEGER,
    post_id INTEGER NOT NULL,
    scan_id INTEGER,
    source TEXT NOT NULL CHECK(
        source IN ('cli', 'web', 'migration') AND length(trim(source)) > 0
    ),
    graded_at TEXT NOT NULL CHECK(
        length(graded_at) = 24
        AND substr(graded_at, 5, 1) = '-'
        AND substr(graded_at, 8, 1) = '-'
        AND substr(graded_at, 11, 1) = 'T'
        AND substr(graded_at, 14, 1) = ':'
        AND substr(graded_at, 17, 1) = ':'
        AND substr(graded_at, 20, 1) = '.'
        AND substr(graded_at, 24, 1) = 'Z'
    ),
    relevance_judgment TEXT NOT NULL CHECK(
        relevance_judgment IN ('correct', 'false_positive', 'false_negative')
    ),
    rejection_reason TEXT,
    comment_quality INTEGER,
    comment_issue TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version IN (1, 2, 3)),
    needs_regrade INTEGER NOT NULL DEFAULT 0 CHECK(needs_regrade IN (0, 1)),
    action_judgment TEXT CHECK(action_judgment IS NULL OR action_judgment IN ('accept', 'fail')),
    dimensions TEXT,
    failure_note TEXT,
    factual_offending_claim TEXT,
    factual_disposition TEXT,
    factual_contradicting_evidence TEXT,
    context_missing_input TEXT,
    posture_should_have_been TEXT,
    implication_implied_claim TEXT,
    implication_missing_support TEXT,
    reply_revision_id INTEGER,
    FOREIGN KEY (evaluation_id) REFERENCES evaluations(id),
    FOREIGN KEY (post_id) REFERENCES posts(id),
    FOREIGN KEY (reply_revision_id) REFERENCES reply_draft_revisions(id)
);

CREATE INDEX IF NOT EXISTS posts_scan_id_idx ON posts(scan_id);
CREATE INDEX IF NOT EXISTS evaluations_scan_id_idx ON evaluations(scan_id);
CREATE INDEX IF NOT EXISTS evaluations_post_id_idx ON evaluations(post_id);
CREATE INDEX IF NOT EXISTS draft_comments_scan_id_idx ON draft_comments(scan_id);
CREATE INDEX IF NOT EXISTS critiques_scan_id_idx ON critiques(scan_id);
CREATE UNIQUE INDEX IF NOT EXISTS draft_comments_evaluation_id_unique
    ON draft_comments(evaluation_id);
CREATE UNIQUE INDEX IF NOT EXISTS critiques_evaluation_id_unique
    ON critiques(evaluation_id) WHERE evaluation_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS grades_evaluation_id_unique
    ON grades(evaluation_id) WHERE evaluation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS grades_scan_id_idx ON grades(scan_id);

CREATE TABLE IF NOT EXISTS projects (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    link TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    dossier_summary_id TEXT
);

CREATE TABLE IF NOT EXISTS project_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key TEXT NOT NULL,
    keyword TEXT NOT NULL,
    evaluate_prompt TEXT,
    respond_prompt TEXT,
    critique_prompt TEXT,
    match_type TEXT NOT NULL DEFAULT 'substring',
    intent TEXT,
    positive_context TEXT,
    negative_context TEXT,
    notes TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 100,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_key) REFERENCES projects(key),
    UNIQUE(project_key, keyword)
);

CREATE INDEX IF NOT EXISTS project_keywords_active_idx
    ON project_keywords(active, priority, id);

CREATE INDEX IF NOT EXISTS project_keywords_project_idx
    ON project_keywords(project_key);

CREATE TABLE IF NOT EXISTS prompt_templates (
    name TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('evaluate', 'respond', 'critique', 'shared', 'custom')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_blocks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    reason_code      TEXT NOT NULL,
    offending_text   TEXT,
    segment_index    INTEGER,
    project_key      TEXT,
    dossier_summary_id TEXT,
    dossier_revision TEXT,
    scan_id          INTEGER,
    post_id          INTEGER,
    evaluation_id    INTEGER,
    context          TEXT,
    created_at       TEXT NOT NULL,
    FOREIGN KEY (scan_id)       REFERENCES scans(id),
    FOREIGN KEY (post_id)       REFERENCES posts(id),
    FOREIGN KEY (evaluation_id) REFERENCES evaluations(id)
);

CREATE INDEX IF NOT EXISTS gate_blocks_scan_id_idx
    ON gate_blocks(scan_id);

CREATE INDEX IF NOT EXISTS gate_blocks_post_id_idx
    ON gate_blocks(post_id);

CREATE INDEX IF NOT EXISTS gate_blocks_evaluation_id_idx
    ON gate_blocks(evaluation_id);

CREATE TABLE IF NOT EXISTS surfaced_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL,
    author_id     TEXT NOT NULL,
    surfaced_at   TEXT NOT NULL,
    post_id       INTEGER,
    evaluation_id INTEGER,
    draft_id      INTEGER,
    project_key   TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (platform, author_id, surfaced_at),
    FOREIGN KEY (post_id)       REFERENCES posts(id),
    FOREIGN KEY (evaluation_id) REFERENCES evaluations(id),
    FOREIGN KEY (draft_id)      REFERENCES draft_comments(id)
);

CREATE INDEX IF NOT EXISTS surfaced_events_author_idx
    ON surfaced_events(author_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS surfaced_events_evaluation_id_unique
    ON surfaced_events(evaluation_id) WHERE evaluation_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS surfaced_events_draft_id_unique
    ON surfaced_events(draft_id) WHERE draft_id IS NOT NULL;

-- Append-only PAA autonomy event stream (added v23, reshaped v33).
-- Current autonomy position for a (task, declaration_version, scope)
-- triple is always derived by folding this table — see
-- paa_service.resolve_current_position — never stored directly. motion_id
-- groups the 1-3 events belonging to one proposal-through-resolution
-- motion; id is that row's own identity.
-- from_position/to_position are required on every event because every
-- writer (propose, approve, reject, demote) copies the full proposal
-- identity onto each event it inserts, including motion_rejected.
--
-- Column order and nullability match paa_runtime.sqlite_store._SCHEMA_DDL
-- exactly as of migration 33, because paa_event_store.ScoutEventStore
-- serves the runtime's EventStore protocol out of this table. event_schema stamps
-- each row with the contract version it was written under. scope is
-- nullable because a declaration that omits `scopes:` resolves at scope
-- None — see the reader queries, which must use `scope IS ?`.
CREATE TABLE IF NOT EXISTS autonomy_events (
    event_schema        TEXT NOT NULL,
    id                  TEXT PRIMARY KEY,
    motion_id           TEXT NOT NULL,
    task                TEXT NOT NULL,
    declaration_version INTEGER NOT NULL,
    scope               TEXT,
    event               TEXT NOT NULL CHECK(event IN (
        'motion_proposed', 'motion_approved', 'motion_rejected', 'position_changed'
    )),
    from_position       TEXT NOT NULL CHECK(from_position IN (
        'manual', 'hitl', 'hotl', 'autonomous'
    )),
    to_position         TEXT NOT NULL CHECK(to_position IN (
        'manual', 'hitl', 'hotl', 'autonomous'
    )),
    evidence_ref        TEXT NOT NULL,
    evidence_sha256     TEXT NOT NULL,
    actor               TEXT NOT NULL,
    reason              TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS autonomy_events_scope_idx
    ON autonomy_events(task, declaration_version, scope, created_at, id);

CREATE INDEX IF NOT EXISTS autonomy_events_motion_idx
    ON autonomy_events(motion_id, created_at, id);

CREATE UNIQUE INDEX IF NOT EXISTS autonomy_events_motion_event_unique
    ON autonomy_events(motion_id, event);

-- Append-only is a hard invariant, not merely a coding convention: these
-- triggers abort any UPDATE or DELETE against autonomy_events regardless
-- of which code path issues it.
CREATE TRIGGER IF NOT EXISTS autonomy_events_no_update
BEFORE UPDATE ON autonomy_events
BEGIN
    SELECT RAISE(ABORT, 'autonomy_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS autonomy_events_no_delete
BEFORE DELETE ON autonomy_events
BEGIN
    SELECT RAISE(ABORT, 'autonomy_events is append-only');
END;

-- Operator-managed author denylist (v25). Scanner posts are retained for
-- audit/deduplication, but active entries are excluded from evaluation and
-- recovery queues before any model calls are made.
CREATE TABLE IF NOT EXISTS blocked_authors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    author_id   TEXT NOT NULL,
    author_name TEXT,
    reason      TEXT,
    active      INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(platform, author_id)
);

CREATE INDEX IF NOT EXISTS blocked_authors_active_idx
    ON blocked_authors(active, platform, author_id);

-- Immutable per-grade revision history (v26). Every authorized grades
-- write appends exactly one row here, atomically with the grades upsert,
-- inside the same transaction. grade_id is the stable audit identity —
-- present even for grades with no current evaluation linkage — while
-- evaluation_id is the linkage snapshot recorded by that revision and may
-- be NULL. 'migration_snapshot' is reserved for the one-time v26 backfill;
-- no live write path ever emits it.
CREATE TABLE IF NOT EXISTS grade_revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    grade_id      INTEGER NOT NULL,
    evaluation_id INTEGER,
    revision      INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    source        TEXT NOT NULL CHECK(
        source IN ('cli', 'web', 'migration', 'migration_snapshot')
        AND length(trim(source)) > 0
    ),
    payload       TEXT NOT NULL,
    recorded_at   TEXT NOT NULL,
    FOREIGN KEY (grade_id) REFERENCES grades(id),
    FOREIGN KEY (evaluation_id) REFERENCES evaluations(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS grade_revisions_grade_id_revision_unique
    ON grade_revisions(grade_id, revision);

CREATE TRIGGER IF NOT EXISTS grade_revisions_no_update
BEFORE UPDATE ON grade_revisions
BEGIN
    SELECT RAISE(ABORT, 'grade_revisions is immutable');
END;

CREATE TRIGGER IF NOT EXISTS grade_revisions_no_delete
BEFORE DELETE ON grade_revisions
BEGIN
    SELECT RAISE(ABORT, 'grade_revisions is immutable');
END;

CREATE TRIGGER IF NOT EXISTS grade_revisions_valid_insert
BEFORE INSERT ON grade_revisions
WHEN json_valid(NEW.payload) = 0
BEGIN
    SELECT RAISE(ABORT, 'grade_revisions.payload must be valid JSON');
END;

-- Current-state usage-override selection metadata (v26), keyed 1:1 by
-- grade_id. Absence of a row reads as mode='auto'. mode='exclude' requires
-- a non-blank reason; there is no force-include value — operators may
-- suppress otherwise-valid evidence but never bypass the schema, contract,
-- linkage, or needs-regrade gates upstream of this table.
CREATE TABLE IF NOT EXISTS grade_usage_overrides (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    grade_id   INTEGER NOT NULL UNIQUE,
    mode       TEXT NOT NULL CHECK(mode IN ('auto', 'exclude')),
    reason     TEXT CHECK(
        (mode = 'auto' AND reason IS NULL)
        OR (mode = 'exclude' AND reason IS NOT NULL AND length(trim(reason)) > 0)
    ),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (grade_id) REFERENCES grades(id)
);

-- Immutable evaluation-feedback/v1 snapshot (v27). One row per scan
-- (scan_id UNIQUE): the scan's resolved policy parameters and the
-- population/eligible/excluded reconciliation counts over the graded_at
-- lookback window. mode records whether the committed phase text was
-- shadow-only or supplied to the corresponding prompt.
CREATE TABLE IF NOT EXISTS feedback_snapshots (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id                  INTEGER NOT NULL UNIQUE,
    policy_version           TEXT NOT NULL,
    mode                     TEXT NOT NULL DEFAULT 'shadow' CHECK(mode IN ('shadow', 'active')),
    as_of                    TEXT NOT NULL,
    lookback_days            INTEGER NOT NULL,
    max_grades               INTEGER NOT NULL,
    segment_min_grades       INTEGER NOT NULL,
    note_max_chars           INTEGER NOT NULL,
    relevance_token_budget   INTEGER NOT NULL,
    reply_draft_token_budget INTEGER NOT NULL,
    critic_token_budget      INTEGER NOT NULL,
    population_count         INTEGER NOT NULL,
    eligible_count           INTEGER NOT NULL,
    excluded_count           INTEGER NOT NULL,
    created_at               TEXT NOT NULL,
    FOREIGN KEY (scan_id) REFERENCES scans(id)
);

CREATE TRIGGER IF NOT EXISTS feedback_snapshots_no_update
BEFORE UPDATE ON feedback_snapshots
BEGIN
    SELECT RAISE(ABORT, 'feedback_snapshots is immutable');
END;

CREATE TRIGGER IF NOT EXISTS feedback_snapshots_no_delete
BEFORE DELETE ON feedback_snapshots
BEGIN
    SELECT RAISE(ABORT, 'feedback_snapshots is immutable');
END;

-- One rendered phase per snapshot (v27): relevance, reply_draft, critic.
-- rendered_text/structured_summary/rendered_sha256/token_estimate are
-- deterministic functions of the phase's projected evidence; an empty
-- eligible population for the phase renders rendered_text='',
-- rendered_sha256 of the empty byte sequence, and token_estimate=0.
CREATE TABLE IF NOT EXISTS feedback_snapshot_phases (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id         INTEGER NOT NULL,
    phase               TEXT NOT NULL CHECK(phase IN ('relevance', 'reply_draft', 'critic')),
    token_budget        INTEGER NOT NULL,
    token_estimate      INTEGER NOT NULL,
    truncated           INTEGER NOT NULL DEFAULT 0 CHECK(truncated IN (0, 1)),
    structured_summary  TEXT NOT NULL,
    rendered_text       TEXT NOT NULL,
    rendered_sha256     TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE(snapshot_id, phase),
    FOREIGN KEY (snapshot_id) REFERENCES feedback_snapshots(id)
);

CREATE TRIGGER IF NOT EXISTS feedback_snapshot_phases_no_update
BEFORE UPDATE ON feedback_snapshot_phases
BEGIN
    SELECT RAISE(ABORT, 'feedback_snapshot_phases is immutable');
END;

CREATE TRIGGER IF NOT EXISTS feedback_snapshot_phases_no_delete
BEFORE DELETE ON feedback_snapshot_phases
BEGIN
    SELECT RAISE(ABORT, 'feedback_snapshot_phases is immutable');
END;

CREATE TRIGGER IF NOT EXISTS feedback_snapshot_phases_valid_insert
BEFORE INSERT ON feedback_snapshot_phases
WHEN json_valid(NEW.structured_summary) = 0
BEGIN
    SELECT RAISE(ABORT, 'feedback_snapshot_phases.structured_summary must be valid JSON');
END;

-- Every in-window grade pinned against exactly one phase row per outcome
-- (v27, explicit selection metadata added in v28): role='excluded' with a reason code for global or
-- not_draft_quality_population exclusions, role='aggregate' for grades
-- contributing to that phase's counts, and an additional role='example'
-- row when a note from that grade was selected for the phase (a grade may
-- carry both an 'aggregate' and an 'example' row in the same phase).
-- grade_revision_id pins the exact grade_revisions row read when the
-- snapshot was built, so a later grade edit cannot rewrite what a past
-- snapshot considered. selection_reason records why each role exists and
-- rank records the policy's 1-based example ordering without relying on
-- insertion IDs.
CREATE TABLE IF NOT EXISTS feedback_snapshot_items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_phase_id  INTEGER NOT NULL,
    grade_id           INTEGER NOT NULL,
    grade_revision_id  INTEGER NOT NULL,
    role               TEXT NOT NULL CHECK(role IN ('excluded', 'aggregate', 'example')),
    reason             TEXT,
    selection_reason   TEXT NOT NULL DEFAULT 'legacy_backfill',
    rank               INTEGER,
    created_at         TEXT NOT NULL,
    UNIQUE(snapshot_phase_id, grade_id, role),
    FOREIGN KEY (snapshot_phase_id) REFERENCES feedback_snapshot_phases(id),
    FOREIGN KEY (grade_id) REFERENCES grades(id),
    FOREIGN KEY (grade_revision_id) REFERENCES grade_revisions(id)
);

CREATE INDEX IF NOT EXISTS feedback_snapshot_items_phase_idx
    ON feedback_snapshot_items(snapshot_phase_id, role);

CREATE TRIGGER IF NOT EXISTS feedback_snapshot_items_no_update
BEFORE UPDATE ON feedback_snapshot_items
BEGIN
    SELECT RAISE(ABORT, 'feedback_snapshot_items is immutable');
END;

CREATE TRIGGER IF NOT EXISTS feedback_snapshot_items_no_delete
BEFORE DELETE ON feedback_snapshot_items
BEGIN
    SELECT RAISE(ABORT, 'feedback_snapshot_items is immutable');
END;

CREATE TRIGGER IF NOT EXISTS feedback_snapshot_items_valid_insert
BEFORE INSERT ON feedback_snapshot_items
WHEN NEW.selection_reason = 'legacy_backfill'
  OR length(trim(NEW.selection_reason)) = 0
  OR (NEW.role = 'example' AND (NEW.rank IS NULL OR NEW.rank <= 0))
  OR (NEW.role != 'example' AND NEW.rank IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'feedback_snapshot_items selection metadata is invalid');
END;

-- One row per phase attempt (v29): the durable link between a relevance,
-- reply_draft, or critic model call and the exact AGENT_RUN Jig trace it
-- produced. Inserted unlinked (evaluation_id NULL) only after the trace
-- has been finalized, flushed, and read back as a verified AGENT_RUN
-- root — never backfilled for historical attempts, which have no stored
-- trace identity to link. A row is linked to the evaluation its output
-- contributed to in the same transaction as that evaluation's insert;
-- evaluation_phase_runs_link_once permits exactly that one NULL->set
-- transition on evaluation_id and rejects every other update.
CREATE TABLE IF NOT EXISTS evaluation_phase_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id            INTEGER NOT NULL,
    post_id            INTEGER NOT NULL,
    evaluation_id      INTEGER,
    snapshot_phase_id  INTEGER NOT NULL,
    phase              TEXT NOT NULL CHECK(phase IN ('relevance', 'reply_draft', 'critic')),
    trace_id           TEXT NOT NULL UNIQUE,
    model              TEXT NOT NULL,
    status             TEXT NOT NULL CHECK(status IN ('complete', 'error', 'cancelled')),
    created_at         TEXT NOT NULL,
    FOREIGN KEY (scan_id) REFERENCES scans(id),
    FOREIGN KEY (post_id) REFERENCES posts(id),
    FOREIGN KEY (evaluation_id) REFERENCES evaluations(id),
    FOREIGN KEY (snapshot_phase_id) REFERENCES feedback_snapshot_phases(id)
);

CREATE INDEX IF NOT EXISTS evaluation_phase_runs_evaluation_idx
    ON evaluation_phase_runs(evaluation_id, created_at, id);

CREATE INDEX IF NOT EXISTS evaluation_phase_runs_snapshot_phase_idx
    ON evaluation_phase_runs(snapshot_phase_id, created_at, id);

CREATE TRIGGER IF NOT EXISTS evaluation_phase_runs_link_once
BEFORE UPDATE ON evaluation_phase_runs
BEGIN
    SELECT RAISE(
        ABORT, 'evaluation_phase_runs rows are append-only except a one-time evaluation_id link'
    )
    WHERE OLD.evaluation_id IS NOT NULL
       OR NEW.evaluation_id IS NULL
       OR NEW.scan_id IS NOT OLD.scan_id
       OR NEW.post_id IS NOT OLD.post_id
       OR NEW.snapshot_phase_id IS NOT OLD.snapshot_phase_id
       OR NEW.phase IS NOT OLD.phase
       OR NEW.trace_id IS NOT OLD.trace_id
       OR NEW.model IS NOT OLD.model
       OR NEW.status IS NOT OLD.status
       OR NEW.created_at IS NOT OLD.created_at;
END;

CREATE TRIGGER IF NOT EXISTS evaluation_phase_runs_no_delete
BEFORE DELETE ON evaluation_phase_runs
BEGIN
    SELECT RAISE(ABORT, 'evaluation_phase_runs is append-only');
END;

-- Versioned parent for the CLI-only offline replay domain (v36): one row
-- per requested candidate configuration, shared across every baseline case
-- (evaluation_phase_runs row) it was replayed against. candidate_config is
-- the frozen v2 candidate-only description (phase/model/system_prompt/
-- system_prompt_sha256/grader_attached) decided before any model call —
-- per-baseline provenance (recorded_input_sha256, baseline_prompt_reused,
-- and reply-correction oracle evidence) lives on each child
-- evaluation_experiments attempt instead, never here. status is a
-- transactionally consistent projection of every linked baseline case's
-- latest attempt (see StateManager._recompute_experiment_run_status),
-- recomputed in the same transaction as every child CAS update — never
-- written directly by any other caller. experiment_runs_no_delete forbids
-- deletion entirely.
CREATE TABLE IF NOT EXISTS experiment_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    status           TEXT NOT NULL
        CHECK(status IN ('queued', 'running', 'complete', 'partial', 'failed')),
    candidate_config TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    completed_at     TEXT
);

CREATE INDEX IF NOT EXISTS experiment_runs_status_idx
    ON experiment_runs(status, created_at, id);

CREATE TRIGGER IF NOT EXISTS experiment_runs_valid_insert
BEFORE INSERT ON experiment_runs
WHEN json_valid(NEW.candidate_config) = 0
  OR NEW.status != 'queued'
  OR NEW.completed_at IS NOT NULL
BEGIN
    SELECT RAISE(
        ABORT, 'experiment_runs must insert as a clean queued row with valid JSON config'
    );
END;

-- name/candidate_config/created_at are immutable once inserted. status and
-- completed_at are the only columns later writers ever change, and only
-- ever via StateManager._recompute_experiment_run_status recomputing the
-- whole projection from scratch inside the same transaction as a child CAS
-- write — a retry that inserts a fresh attempt under a 'partial' or
-- 'failed' run legitimately reopens it back to 'running' (and clears
-- completed_at) once that attempt exists, so status is deliberately NOT
-- forward-only or frozen at a terminal value the way a real lifecycle
-- column would be.
CREATE TRIGGER IF NOT EXISTS experiment_runs_immutable_identity
BEFORE UPDATE ON experiment_runs
BEGIN
    SELECT RAISE(ABORT, 'experiment_runs name/candidate_config/created_at cannot change')
    WHERE NEW.name IS NOT OLD.name
       OR NEW.candidate_config IS NOT OLD.candidate_config
       OR NEW.created_at IS NOT OLD.created_at;
END;

CREATE TRIGGER IF NOT EXISTS experiment_runs_no_delete
BEFORE DELETE ON experiment_runs
BEGIN
    SELECT RAISE(ABORT, 'experiment_runs rows cannot be deleted');
END;

-- Immutable per-baseline-case attempt child of experiment_runs (v36,
-- rebuilt from the v30-v35 one-off evaluation_experiments shape). One row
-- per (experiment_run_id, phase_run_id, attempt_number): attempt_number is
-- 1 for a baseline case's first try under a run and increments for each
-- retry, which always inserts a brand-new row rather than mutating the
-- attempt it retries — supersedes_experiment_id names that prior attempt
-- so retry lineage is explicit while every earlier attempt's trace,
-- comparison, error, usage, and cost evidence stays immutable. Always
-- inserted as a clean 'queued' row (evaluation_experiments_valid_insert);
-- baseline_evidence is the frozen v2 per-baseline provenance (at minimum
-- recorded_input_sha256/baseline_prompt_reused; a reply_draft-eligible
-- attempt additionally pins the correction oracle — reply_revision_id,
-- correction_sha256, project_key, dossier_summary_id, dossier_revision,
-- baseline_model, baseline_prompt_sha256, grader_version,
-- assembler_version) and, like every other identity column here, is fixed
-- at insert and never changed. candidate_trace_id/candidate_llm_call_count/
-- candidate_cost are populated by a later CAS update once the candidate
-- AGENT_RUN trace is generated, flushed, and verified — never guessed or
-- backfilled. error_detail is a capped, sanitized operator-safe message.
-- evaluation_experiments_lifecycle permits only queued->running and
-- running->running/complete/failed; no row may leave complete/failed, and
-- evaluation_experiments_no_delete forbids deletion entirely.
CREATE TABLE IF NOT EXISTS evaluation_experiments (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_run_id        INTEGER NOT NULL,
    phase_run_id             INTEGER NOT NULL,
    attempt_number           INTEGER NOT NULL CHECK(attempt_number >= 1),
    supersedes_experiment_id INTEGER,
    status                   TEXT NOT NULL
        CHECK(status IN ('queued', 'running', 'complete', 'failed')),
    baseline_evidence        TEXT NOT NULL,
    candidate_trace_id       TEXT UNIQUE,
    candidate_llm_call_count INTEGER
        CHECK(candidate_llm_call_count IS NULL OR candidate_llm_call_count >= 0),
    candidate_cost           REAL CHECK(candidate_cost IS NULL OR candidate_cost >= 0),
    error_detail             TEXT CHECK(error_detail IS NULL OR length(error_detail) <= 2000),
    created_at               TEXT NOT NULL,
    completed_at             TEXT,
    UNIQUE(experiment_run_id, phase_run_id, attempt_number),
    FOREIGN KEY (experiment_run_id) REFERENCES experiment_runs(id),
    FOREIGN KEY (phase_run_id) REFERENCES evaluation_phase_runs(id),
    FOREIGN KEY (supersedes_experiment_id) REFERENCES evaluation_experiments(id)
);

CREATE INDEX IF NOT EXISTS evaluation_experiments_run_idx
    ON evaluation_experiments(experiment_run_id, created_at, id);

CREATE INDEX IF NOT EXISTS evaluation_experiments_phase_run_idx
    ON evaluation_experiments(phase_run_id, created_at, id);

CREATE INDEX IF NOT EXISTS evaluation_experiments_status_idx
    ON evaluation_experiments(status, created_at, id);

CREATE TRIGGER IF NOT EXISTS evaluation_experiments_valid_insert
BEFORE INSERT ON evaluation_experiments
WHEN json_valid(NEW.baseline_evidence) = 0
  OR NEW.status != 'queued'
  OR NEW.candidate_trace_id IS NOT NULL
  OR NEW.candidate_llm_call_count IS NOT NULL
  OR NEW.candidate_cost IS NOT NULL
  OR NEW.error_detail IS NOT NULL
  OR NEW.completed_at IS NOT NULL
BEGIN
    SELECT RAISE(
        ABORT,
        'evaluation_experiments must insert as a clean queued attempt with valid JSON evidence'
    );
END;

CREATE TRIGGER IF NOT EXISTS evaluation_experiments_lifecycle
BEFORE UPDATE ON evaluation_experiments
BEGIN
    SELECT RAISE(
        ABORT,
        'evaluation_experiments attempts only allow queued->running->complete/failed CAS updates'
    )
    WHERE OLD.status IN ('complete', 'failed')
       OR NEW.experiment_run_id IS NOT OLD.experiment_run_id
       OR NEW.phase_run_id IS NOT OLD.phase_run_id
       OR NEW.attempt_number IS NOT OLD.attempt_number
       OR NEW.supersedes_experiment_id IS NOT OLD.supersedes_experiment_id
       OR NEW.baseline_evidence IS NOT OLD.baseline_evidence
       OR NEW.created_at IS NOT OLD.created_at
       OR (OLD.candidate_trace_id IS NOT NULL
           AND NEW.candidate_trace_id IS NOT OLD.candidate_trace_id)
       OR (OLD.candidate_llm_call_count IS NOT NULL
           AND NEW.candidate_llm_call_count IS NOT OLD.candidate_llm_call_count)
       OR (OLD.candidate_cost IS NOT NULL AND NEW.candidate_cost IS NOT OLD.candidate_cost)
       OR NOT (
            (OLD.status = 'queued' AND NEW.status = 'running')
         OR (OLD.status = 'running' AND NEW.status IN ('running', 'complete', 'failed'))
       );
END;

CREATE TRIGGER IF NOT EXISTS evaluation_experiments_no_delete
BEFORE DELETE ON evaluation_experiments
BEGIN
    SELECT RAISE(ABORT, 'evaluation_experiments rows cannot be deleted');
END;

-- Immutable, insert-only comparison evidence for one completed replay
-- attempt (v31, score_evidence added v36): first-class baseline/candidate
-- trace identities, the pinned Jig commit that produced trace_diff, the
-- native jig.replay.diff.TraceDiff JSON (left untouched — Jig's own
-- native trace comparison never learns about Scout's grader), and Scout's
-- own canonical domain_diff built only from complete baseline/candidate
-- structured values. score_evidence is populated only for a candidate
-- that ran with Scout's reply-correction grader attached (grader_attached
-- true in the child's baseline_evidence): the versioned grader/assembler
-- identity, the correction hash/revision it was pinned against, and both
-- the independently computed historical baseline_distance and the
-- candidate_distance the live grader produced, plus their delta. NULL for
-- every ungraded (relevance/critic, or grader-ineligible) comparison.
-- Exactly one row may exist per attempt (UNIQUE(experiment_id)), inserted
-- in the same transaction as the CAS to 'complete' so a complete attempt
-- always implies durable, valid comparison evidence and vice versa. Never
-- updated or deleted once written.
CREATE TABLE IF NOT EXISTS trace_comparisons (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  INTEGER NOT NULL UNIQUE,
    trace_a_id     TEXT NOT NULL,
    trace_b_id     TEXT NOT NULL,
    jig_revision   TEXT NOT NULL,
    trace_diff     TEXT NOT NULL,
    domain_diff    TEXT NOT NULL,
    score_evidence TEXT,
    created_at     TEXT NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES evaluation_experiments(id)
);

CREATE INDEX IF NOT EXISTS trace_comparisons_trace_a_idx
    ON trace_comparisons(trace_a_id, created_at, id);

CREATE INDEX IF NOT EXISTS trace_comparisons_trace_b_idx
    ON trace_comparisons(trace_b_id, created_at, id);

CREATE TRIGGER IF NOT EXISTS trace_comparisons_valid_insert
BEFORE INSERT ON trace_comparisons
WHEN json_valid(NEW.trace_diff) = 0
  OR json_valid(NEW.domain_diff) = 0
  OR (NEW.score_evidence IS NOT NULL AND json_valid(NEW.score_evidence) = 0)
BEGIN
    SELECT RAISE(ABORT, 'trace_comparisons JSON columns must be valid JSON');
END;

CREATE TRIGGER IF NOT EXISTS trace_comparisons_identity_insert
BEFORE INSERT ON trace_comparisons
WHEN json_valid(NEW.trace_diff) = 1
 AND (
     json_extract(NEW.trace_diff, '$.trace_a_id') IS NOT NEW.trace_a_id
  OR json_extract(NEW.trace_diff, '$.trace_b_id') IS NOT NEW.trace_b_id
  OR NOT EXISTS (
      SELECT 1
      FROM evaluation_experiments e
      JOIN evaluation_phase_runs pr ON pr.id = e.phase_run_id
      WHERE e.id = NEW.experiment_id
        AND e.status = 'running'
        AND pr.trace_id = NEW.trace_a_id
        AND e.candidate_trace_id = NEW.trace_b_id
  ))
BEGIN
    SELECT RAISE(ABORT, 'trace_comparisons trace identities do not match experiment evidence');
END;

CREATE TRIGGER IF NOT EXISTS trace_comparisons_no_update
BEFORE UPDATE ON trace_comparisons
BEGIN
    SELECT RAISE(ABORT, 'trace_comparisons is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trace_comparisons_no_delete
BEFORE DELETE ON trace_comparisons
BEGIN
    SELECT RAISE(ABORT, 'trace_comparisons is immutable');
END;

-- Durable workflow state for a human override of a model-negative
-- evaluation (v32). The source evaluation retains the false-negative
-- grade; the target evaluation is the separate draft/critic outcome that
-- can later receive its own ordinary response-quality grade.
CREATE TABLE IF NOT EXISTS human_positive_promotions (
    source_evaluation_id INTEGER PRIMARY KEY,
    source_grade_id      INTEGER NOT NULL,
    scan_id              INTEGER,
    target_evaluation_id INTEGER,
    status               TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
    error_detail         TEXT CHECK(error_detail IS NULL OR length(error_detail) <= 2000),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    completed_at         TEXT,
    CHECK(
        (status = 'completed' AND scan_id IS NOT NULL
            AND target_evaluation_id IS NOT NULL AND completed_at IS NOT NULL
            AND error_detail IS NULL)
        OR status IN ('running', 'failed')
    ),
    FOREIGN KEY (source_evaluation_id) REFERENCES evaluations(id),
    FOREIGN KEY (source_grade_id) REFERENCES grades(id),
    FOREIGN KEY (scan_id) REFERENCES scans(id),
    FOREIGN KEY (target_evaluation_id) REFERENCES evaluations(id)
);

CREATE INDEX IF NOT EXISTS human_positive_promotions_status_idx
    ON human_positive_promotions(status, updated_at, source_evaluation_id);

PRAGMA user_version = {LATEST_SCHEMA_VERSION};
"""
