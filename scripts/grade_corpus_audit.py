"""Read-only, reproducible audit of the grade corpus, plus a separately
authorized remediation path.

The audit half never mutates: it requires a SQLite URI containing
``mode=ro``, additionally sets ``PRAGMA query_only=ON``, and reads every
row inside one consistent read transaction. It classifies every grade row
into zero or more known corruption modes and emits both a machine-readable
manifest (JSON) and a human-readable Markdown report rendered purely from
that manifest — Markdown is never a second set of database queries.

The remediation half is a distinct, explicitly authorized mutation: it
requires ``--apply``, a writable database path, and the exact manifest
produced by a prior audit run. It reclassifies the corpus, verifies the
manifest has not drifted, flags every known-bad row via ``needs_regrade``,
optionally applies a reviewed replacement manifest through the validated
transaction-aware StateManager writer, proves downstream exclusion, and
commits everything as one unit — otherwise it aborts without mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scout.config import HUMAN_GRADE_SCHEMA_VERSION, GradeRecord
from scout.grading.service import validate_grade_envelope
from scout.storage.state import (
    LATEST_SCHEMA_VERSION,
    GradeValidationError,
    StateManager,
    grade_revision_comparison_shape,
    parse_graded_at,
)

REPORT_SCHEMA_VERSION = 1
CONVERGENCE_REPORT_SCHEMA_VERSION = 1

CONVERGED = "converged"
MISSING_REVISION = "missing_revision"
DIVERGENT_REVISION = "divergent_revision"

# Classes with at most this many unique rows also receive manual_regrade
# disposition after flagging; larger classes stay flag-only.
MANUAL_REGRADE_THRESHOLD = 20

PLACEHOLDER_FAILURE_NOTE = "no-note-provided"

CAUSAL_DETAIL_FIELDS = (
    "factual_offending_claim",
    "factual_disposition",
    "factual_contradicting_evidence",
    "context_missing_input",
    "posture_should_have_been",
    "implication_implied_claim",
    "implication_missing_support",
)

VALIDATOR_FAILURE = "validator_failure"
INVERTED_WEB_SEMANTICS = "inverted_web_semantics"
MANUFACTURED_CLI_DATA = "manufactured_cli_data"
NOOP_POSTURE_CORRECTION = "noop_posture_correction"
HISTORICAL_ABSTAIN_CONFUSION = "historical_abstain_confusion"

CLASS_IDS = (
    VALIDATOR_FAILURE,
    INVERTED_WEB_SEMANTICS,
    MANUFACTURED_CLI_DATA,
    NOOP_POSTURE_CORRECTION,
    HISTORICAL_ABSTAIN_CONFUSION,
)

_GRADE_JOIN_EVAL_SELECT = (
    "SELECT g.id, g.evaluation_id, g.post_id, g.scan_id, g.source, g.graded_at, "
    "g.relevance_judgment, g.action_judgment, g.schema_version, g.needs_regrade, "
    "g.dimensions, g.failure_note, g.factual_offending_claim, g.factual_disposition, "
    "g.factual_contradicting_evidence, g.context_missing_input, "
    "g.posture_should_have_been, g.implication_implied_claim, "
    "g.implication_missing_support, "
    "e.id AS eval_row_id, e.post_id AS eval_post_id, e.scan_id AS eval_scan_id, "
    "e.relevant AS eval_relevant, e.posture AS eval_posture, "
    "e.surface_status AS eval_surface_status "
    "FROM grades g LEFT JOIN evaluations e ON e.id = g.evaluation_id "
    "ORDER BY g.id"
)

# grade_revision_comparison_shape's edited_text field is not a grades
# column — it lives in reply_draft_revisions.reply_text, addressed by
# reply_revision_id — so every convergence query below joins it in under
# that alias. Without this, a v3 grade with a real correction would
# compare as edited_text=None against its own recorded revision and
# misreport as divergent.
# No trailing space here — callers append their own clause with an
# explicit leading space, so the join stays syntactically correct
# regardless of how either string is edited later.
_GRADE_WITH_EDITED_TEXT_SELECT = (
    "SELECT g.*, rdr.reply_text AS edited_text FROM grades g "
    "LEFT JOIN reply_draft_revisions rdr ON rdr.id = g.reply_revision_id"
)


class AuditError(Exception):
    """Raised for CLI-facing audit/remediation usage errors."""


def canonical_json(value: object) -> str:
    """Stable JSON encoding used for the manifest and digest input."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def utc_canonical(when: datetime) -> str:
    """Render a UTC datetime as ``YYYY-MM-DDTHH:MM:SS.mmmZ``."""
    if when.tzinfo is None:
        raise ValueError("utc_canonical requires a timezone-aware datetime")
    as_utc = when.astimezone(UTC)
    millis = as_utc.microsecond // 1000
    return as_utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"


def open_readonly_connection(db_uri: str) -> sqlite3.Connection:
    """Open a strictly read-only SQLite connection.

    Raises AuditError if the URI does not request read-only mode. This is
    a separate, OS-level read-only guarantee from `Db.read_transaction()`
    (the URI's `mode=ro` means SQLite cannot write to the file at all,
    regardless of application logic). Audit entry points explicitly open a
    read transaction around manifest construction so every read observes one
    snapshot.
    """
    if "mode=ro" not in db_uri:
        raise AuditError(f"db_uri must include mode=ro, got {db_uri!r}")
    conn = sqlite3.connect(db_uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _load_dimensions(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(d) for d in parsed]


def classify_row(row: sqlite3.Row) -> frozenset[str]:
    """Classify a single grades row (left-joined to its evaluation, if any)
    into zero or more known corruption modes."""
    classes: set[str] = set()
    dims = _load_dimensions(row["dimensions"])
    has_evaluation = row["eval_row_id"] is not None

    if row["schema_version"] == 2:
        if not has_evaluation or (
            row["eval_post_id"] != row["post_id"]
            or (
                row["scan_id"] is not None
                and row["eval_scan_id"] is not None
                and row["scan_id"] != row["eval_scan_id"]
            )
        ):
            classes.add(VALIDATOR_FAILURE)
        else:
            # These are historical schema_version=2 rows, which never had an
            # edited_text column — the shared contract's v3 additions
            # (edited_text itself, and the relaxed no-failure_note-required
            # causal-detail-only fail shape) are consulted here as they are
            # everywhere else; a v2 row can only ever satisfy the relaxed
            # branch by genuinely carrying valid causal detail, which was
            # already required of it under the old rules too.
            errors = validate_grade_envelope(
                {
                    "relevance_judgment": row["relevance_judgment"],
                    "action_judgment": row["action_judgment"],
                    "dimensions": dims or None,
                    "failure_note": row["failure_note"],
                    "factual_offending_claim": row["factual_offending_claim"],
                    "factual_disposition": row["factual_disposition"],
                    "factual_contradicting_evidence": row["factual_contradicting_evidence"],
                    "context_missing_input": row["context_missing_input"],
                    "posture_should_have_been": row["posture_should_have_been"],
                    "implication_implied_claim": row["implication_implied_claim"],
                    "implication_missing_support": row["implication_missing_support"],
                    "edited_text": None,
                },
                row["eval_posture"],
            )
            if errors:
                classes.add(VALIDATOR_FAILURE)

    if row["source"] == "web" and has_evaluation and row["eval_relevant"] == 0:
        classes.add(INVERTED_WEB_SEMANTICS)

    if row["source"] == "cli":
        note = row["failure_note"]
        if note == PLACEHOLDER_FAILURE_NOTE:
            classes.add(MANUFACTURED_CLI_DATA)
        elif dims == ["usefulness"]:
            note_is_placeholder = (
                not note or not note.strip() or note.strip() == PLACEHOLDER_FAILURE_NOTE
            )
            no_causal_detail = all(row[field] is None for field in CAUSAL_DETAIL_FIELDS)
            if note_is_placeholder and no_causal_detail:
                classes.add(MANUFACTURED_CLI_DATA)

    if (
        "posture" in dims
        and has_evaluation
        and row["posture_should_have_been"] is not None
        and row["posture_should_have_been"] == row["eval_posture"]
    ):
        classes.add(NOOP_POSTURE_CORRECTION)

    if (
        has_evaluation
        and row["eval_posture"] == "abstain"
        and row["eval_surface_status"] == "not_relevant"
    ):
        classes.add(HISTORICAL_ABSTAIN_CONFUSION)

    return frozenset(classes)


@dataclass(frozen=True)
class Classification:
    """The full per-row classification of a corpus snapshot, plus the
    aggregated per-class and cross-class views derived from it."""

    total_eligible_grades: int
    row_classes: dict[int, frozenset[str]]
    row_evaluation_ids: dict[int, int | None]

    def grade_ids_for(self, class_id: str) -> list[int]:
        return sorted(gid for gid, classes in self.row_classes.items() if class_id in classes)

    def evaluation_ids_for(self, class_id: str) -> list[int]:
        ids: set[int] = {
            evaluation_id
            for gid, classes in self.row_classes.items()
            if class_id in classes
            and (evaluation_id := self.row_evaluation_ids[gid]) is not None
        }
        return sorted(ids)

    def affected_grade_ids(self) -> list[int]:
        return sorted(gid for gid, classes in self.row_classes.items() if classes)

    def affected_evaluation_ids(self) -> list[int]:
        ids: set[int] = {
            evaluation_id
            for gid, classes in self.row_classes.items()
            if classes and (evaluation_id := self.row_evaluation_ids[gid]) is not None
        }
        return sorted(ids)


def classify_corpus(conn: sqlite3.Connection) -> Classification:
    """Run classify_row over every grade in the corpus visible to conn."""
    rows = conn.execute(_GRADE_JOIN_EVAL_SELECT).fetchall()
    row_classes: dict[int, frozenset[str]] = {}
    row_evaluation_ids: dict[int, int | None] = {}
    for row in rows:
        row_classes[row["id"]] = classify_row(row)
        row_evaluation_ids[row["id"]] = row["evaluation_id"]
    return Classification(
        total_eligible_grades=len(rows),
        row_classes=row_classes,
        row_evaluation_ids=row_evaluation_ids,
    )


def _class_disposition(grade_ids: list[int]) -> str:
    if grade_ids and len(grade_ids) <= MANUAL_REGRADE_THRESHOLD:
        return "flag_and_manual_regrade"
    return "flag"


def _content_digest(
    classes_report: dict[str, Any],
    affected_grade_ids: list[int],
    db_schema_version: int,
    total_eligible_grades: int,
) -> str:
    payload = {
        "classes": classes_report,
        "affected_grade_ids": affected_grade_ids,
        "db_schema_version": db_schema_version,
        "total_eligible_grades": total_eligible_grades,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_manifest(conn: sqlite3.Connection, *, db_target: str, now: datetime) -> dict[str, Any]:
    """Build the machine-readable audit manifest. Pure given (conn, now) —
    issues no writes."""
    schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    classification = classify_corpus(conn)

    classes_report: dict[str, Any] = {}
    for class_id in CLASS_IDS:
        grade_ids = classification.grade_ids_for(class_id)
        classes_report[class_id] = {
            "grade_ids": grade_ids,
            "evaluation_ids": classification.evaluation_ids_for(class_id),
            "count": len(grade_ids),
            "disposition": _class_disposition(grade_ids),
        }

    intersections: dict[str, list[int]] = {}
    for i, class_a in enumerate(CLASS_IDS):
        set_a = set(classes_report[class_a]["grade_ids"])
        for class_b in CLASS_IDS[i + 1 :]:
            set_b = set(classes_report[class_b]["grade_ids"])
            overlap = sorted(set_a & set_b)
            intersections[f"{class_a}__{class_b}"] = overlap

    affected_grade_ids = classification.affected_grade_ids()

    digest = _content_digest(
        classes_report, affected_grade_ids, schema_version, classification.total_eligible_grades
    )

    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "run_at": utc_canonical(now),
        "db_target": db_target,
        "db_schema_version": schema_version,
        "total_eligible_grades": classification.total_eligible_grades,
        "classes": classes_report,
        "intersections": intersections,
        "affected_total": len(affected_grade_ids),
        "affected_grade_ids": affected_grade_ids,
        "affected_evaluation_ids": classification.affected_evaluation_ids(),
        "digest": digest,
    }


def run_audit(db_uri: str, *, now: datetime) -> dict[str, Any]:
    """Open db_uri read-only and build the manifest. Raises AuditError if
    db_uri is not read-only."""
    conn = open_readonly_connection(db_uri)
    try:
        conn.execute("BEGIN")
        return build_manifest(conn, db_target=db_uri, now=now)
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def render_markdown(manifest: dict[str, Any]) -> str:
    """Render the comms report from the manifest object. No DB access —
    this is purely a rendering of the manifest."""
    lines = [
        "# Grade Corpus Audit",
        "",
        f"- Run at: {manifest['run_at']}",
        f"- Database target: `{manifest['db_target']}`",
        f"- Database schema version: {manifest['db_schema_version']}",
        f"- Report schema version: {manifest['report_schema_version']}",
        f"- Total eligible grades: {manifest['total_eligible_grades']}",
        f"- Deduplicated affected total: {manifest['affected_total']}",
        f"- Manifest digest: `{manifest['digest']}`",
        "",
        "## Classes",
        "",
    ]

    for class_id in CLASS_IDS:
        entry = manifest["classes"][class_id]
        lines.append(f"### `{class_id}`")
        lines.append("")
        lines.append(f"- Count: {entry['count']}")
        lines.append(f"- Disposition: {entry['disposition']}")
        lines.append(f"- Grade IDs: {entry['grade_ids']}")
        lines.append(f"- Evaluation IDs: {entry['evaluation_ids']}")
        lines.append("")

    lines.append("## Intersections")
    lines.append("")
    any_overlap = False
    for pair, overlap in manifest["intersections"].items():
        if overlap:
            any_overlap = True
            lines.append(f"- `{pair}`: {overlap}")
    if not any_overlap:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Disposition")
    lines.append("")
    lines.append(
        "Every automatically detected known-bad row is flagged via "
        "`needs_regrade=1` before use. Classes with at most "
        f"{MANUAL_REGRADE_THRESHOLD} unique rows are additionally assigned "
        "manual regrade after flagging; larger classes remain excluded "
        "until a separately reviewed regrade batch exists."
    )
    lines.append("")

    if "post_remediation" in manifest:
        post = manifest["post_remediation"]
        lines.append("## Post-remediation verification")
        lines.append("")
        lines.append(f"- Grades flagged this run: {post['flagged_count']}")
        lines.append(f"- Replacements applied: {post['replacements_applied']}")
        lines.append(
            "- `get_recent_grading_signals` reachable known-bad evaluation IDs: "
            f"{post['signal_reachable_count']}"
        )
        lines.append(
            "- `export_eval_cases` reachable known-bad evaluation IDs: "
            f"{post['export_reachable_count']}"
        )
        lines.append("")

    return "\n".join(lines) + "\n"


# --- Remediation ---


def _replacement_to_grade_record(
    entry: dict[str, Any], *, post_id: int, scan_id: int | None, evaluation_id: int | None
) -> GradeRecord:
    graded_at = parse_graded_at(entry["graded_at"]) if "graded_at" in entry else datetime.now(UTC)
    return GradeRecord(
        post_id=post_id,
        evaluation_id=evaluation_id,
        scan_id=scan_id,
        source=entry.get("source", "cli"),
        graded_at=graded_at,
        relevance_judgment=entry["relevance_judgment"],
        schema_version=HUMAN_GRADE_SCHEMA_VERSION,
        needs_regrade=False,
        action_judgment=entry.get("action_judgment"),
        dimensions=entry.get("dimensions"),
        failure_note=entry.get("failure_note"),
        factual_offending_claim=entry.get("factual_offending_claim"),
        factual_disposition=entry.get("factual_disposition"),
        factual_contradicting_evidence=entry.get("factual_contradicting_evidence"),
        context_missing_input=entry.get("context_missing_input"),
        posture_should_have_been=entry.get("posture_should_have_been"),
        implication_implied_claim=entry.get("implication_implied_claim"),
        implication_missing_support=entry.get("implication_missing_support"),
    )


def get_recent_grading_signal_evaluation_ids(
    conn: sqlite3.Connection, limit_scans: int = 3
) -> set[int]:
    """The exact evaluation IDs that feed StateManager.get_recent_grading_signals,
    for reachability verification instead of its aggregated counts."""
    rows = conn.execute(
        "SELECT e.id AS evaluation_id FROM grades g "
        "JOIN evaluations e ON e.id = g.evaluation_id "
        f"WHERE g.schema_version = {HUMAN_GRADE_SCHEMA_VERSION} AND g.needs_regrade = 0 "
        "AND e.scan_id IN ("
        "  SELECT id FROM scans WHERE completed_at IS NOT NULL "
        "  ORDER BY id DESC LIMIT ?"
        ")",
        (limit_scans,),
    ).fetchall()
    return {int(r["evaluation_id"]) for r in rows}


def _verify_no_drift(current: dict[str, Any], manifest: dict[str, Any]) -> None:
    if current["digest"] != manifest["digest"]:
        raise AuditError(
            "audit manifest has drifted from current database state — "
            f"expected digest {manifest['digest']}, got {current['digest']}"
        )
    if current["affected_grade_ids"] != manifest["affected_grade_ids"]:
        raise AuditError(
            "audit manifest row IDs have drifted from current database state"
        )


def remediate(
    db_path: str,
    manifest: dict[str, Any],
    *,
    apply: bool,
    replacement_manifest: dict[str, Any] | None = None,
    now: datetime,
) -> dict[str, Any]:
    """Verify, and — only with ``apply=True`` — flag, replace, and prove
    downstream exclusion, all as one atomic unit.

    Without ``--apply`` the manifest/drift checks run inside
    ``state.db.read_transaction()``: a mechanically read-only snapshot
    that can perform no writes and always ends with rollback, so a dry
    run can never leave a partial or leaked lock behind. Apply mode runs
    the same checks plus every flag/replacement mutation inside one
    ``state.db.begin_immediate()`` unit with no I/O inside it, committing
    only after every reviewed replacement passes the live contract and no
    *currently* known-bad row is reachable by the prompt/export readers;
    any failure rolls the whole unit back. A database below the current
    schema must be upgraded and re-audited first; remediation never
    performs an implicit migration before checking the audit digest.
    """
    db_uri = Path(db_path).resolve().as_uri() + "?mode=rw"
    try:
        preflight = sqlite3.connect(db_uri, uri=True)
    except sqlite3.OperationalError as exc:
        raise AuditError(f"cannot open writable database {db_path!r}: {exc}") from exc
    try:
        db_schema_version = int(preflight.execute("PRAGMA user_version").fetchone()[0])
    finally:
        preflight.close()
    try:
        manifest_schema_version = int(manifest.get("db_schema_version", -1))
    except (TypeError, ValueError) as exc:
        raise AuditError(
            "audit manifest db_schema_version must be an integer"
        ) from exc
    if db_schema_version != manifest_schema_version:
        raise AuditError(
            "audit manifest schema version has drifted from the writable database — "
            f"expected {manifest_schema_version}, got {db_schema_version}"
        )
    if db_schema_version != LATEST_SCHEMA_VERSION:
        raise AuditError(
            "remediation requires the latest database schema before StateManager opens it; "
            f"upgrade to {LATEST_SCHEMA_VERSION}, then rerun the read-only audit"
        )

    state = StateManager(db_path)
    try:
        if not apply:
            with state.db.read_transaction():
                current = build_manifest(state.conn, db_target=db_path, now=now)
                _verify_no_drift(current, manifest)
                affected_grade_ids: list[int] = manifest["affected_grade_ids"]
                return {
                    "dry_run": True,
                    "would_flag_count": len(affected_grade_ids),
                    "would_flag_grade_ids": affected_grade_ids,
                    "digest_verified": True,
                }

        with state.db.begin_immediate():
            current = build_manifest(state.conn, db_target=db_path, now=now)
            _verify_no_drift(current, manifest)

            affected_grade_ids = manifest["affected_grade_ids"]

            for grade_id in affected_grade_ids:
                state.mark_grade_needs_regrade_for_remediation(
                    grade_id,
                    remediation_reason=f"grade corpus audit {manifest['digest']}",
                )

            replacements_applied = 0
            if replacement_manifest:
                affected_set = set(affected_grade_ids)
                for grade_id_str, entry in replacement_manifest.items():
                    try:
                        grade_id = int(grade_id_str)
                    except (TypeError, ValueError) as exc:
                        raise AuditError(
                            f"replacement manifest has invalid grade id {grade_id_str!r}"
                        ) from exc
                    if grade_id not in affected_set:
                        raise AuditError(
                            f"replacement manifest references grade id {grade_id}, "
                            "which is not in the audited affected set"
                        )
                    row = state.conn.execute(
                        "SELECT post_id, scan_id, evaluation_id FROM grades WHERE id = ?",
                        (grade_id,),
                    ).fetchone()
                    if row is None:
                        raise AuditError(
                            f"replacement manifest references unknown grade id {grade_id}"
                        )
                    record = _replacement_to_grade_record(
                        entry,
                        post_id=row["post_id"],
                        scan_id=row["scan_id"],
                        evaluation_id=row["evaluation_id"],
                    )
                    try:
                        state.save_grade_for_remediation(
                            record,
                            remediation_reason=f"grade corpus audit {manifest['digest']}",
                        )
                    except GradeValidationError as exc:
                        raise AuditError(
                            f"replacement for grade id {grade_id} failed validation: {exc}"
                        ) from exc
                    replacements_applied += 1

            post_classification = classify_corpus(state.conn)
            known_bad_eval_ids = set(post_classification.affected_evaluation_ids())
            signal_reachable = (
                known_bad_eval_ids
                & get_recent_grading_signal_evaluation_ids(state.conn)
            )
            export_eval_ids: set[int] = set()
            for case in state.export_eval_cases():
                evaluation = case["evaluation"]
                assert isinstance(evaluation, dict)
                evaluation_id = evaluation["evaluation_id"]
                if evaluation_id is not None:
                    export_eval_ids.add(int(evaluation_id))
            export_reachable = known_bad_eval_ids & export_eval_ids
            if signal_reachable or export_reachable:
                raise AuditError(
                    "post-remediation verification found known-bad rows still reachable "
                    f"(signals={sorted(signal_reachable)}, export={sorted(export_reachable)})"
                )

            return {
                "dry_run": False,
                "flagged_count": len(affected_grade_ids),
                "flagged_grade_ids": affected_grade_ids,
                "replacements_applied": replacements_applied,
                "remaining_known_bad_count": len(post_classification.affected_grade_ids()),
                "signal_reachable_count": len(signal_reachable),
                "signal_reachable_evaluation_ids": sorted(signal_reachable),
                "export_reachable_count": len(export_reachable),
                "export_reachable_evaluation_ids": sorted(export_reachable),
            }
    finally:
        state.close()


# --- Revision convergence audit + repair ---
#
# Distinct from the corruption-class audit/remediate above: this checks
# whether every grade's grade_revisions history actually converges on its
# current grades row, not whether the row's own content is valid. The
# read-only audit and the explicitly authorized repair share exactly one
# canonicalization function — state_manager.grade_revision_comparison_shape
# — so the two can never disagree about what "converged" means. Repair
# never rewrites or deletes existing grade_revisions rows (the database
# enforces that at the trigger level regardless); it only ever appends one
# more honest revision, and only when a grade is still missing one or
# still diverges once rechecked under the write lock.


def _latest_revision_payload(conn: sqlite3.Connection, grade_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload FROM grade_revisions WHERE grade_id = ? ORDER BY revision DESC LIMIT 1",
        (grade_id,),
    ).fetchone()
    if row is None:
        return None
    payload: dict[str, Any] = json.loads(row["payload"])
    return payload


def _classify_one_convergence(
    current_shape: dict[str, Any], latest_payload: dict[str, Any] | None
) -> tuple[str, list[str]]:
    """Return (status, sorted mismatched field keys). Mismatched keys are
    empty for converged/missing_revision — there is nothing to compare a
    missing revision against."""
    if latest_payload is None:
        return MISSING_REVISION, []
    if latest_payload == current_shape:
        return CONVERGED, []
    mismatched = sorted(
        key
        for key in set(current_shape) | set(latest_payload)
        if current_shape.get(key) != latest_payload.get(key)
    )
    return DIVERGENT_REVISION, mismatched


def classify_convergence(conn: sqlite3.Connection) -> dict[int, tuple[str, list[str]]]:
    """Classify every grade's grade_revisions convergence. Read-only — a
    single pass over grades plus one latest-revision lookup per grade."""
    result: dict[int, tuple[str, list[str]]] = {}
    for row in conn.execute(_GRADE_WITH_EDITED_TEXT_SELECT + " ORDER BY g.id").fetchall():
        grade_id = int(row["id"])
        current_shape = grade_revision_comparison_shape(row)
        latest_payload = _latest_revision_payload(conn, grade_id)
        result[grade_id] = _classify_one_convergence(current_shape, latest_payload)
    return result


def build_convergence_manifest(
    conn: sqlite3.Connection, *, db_target: str, now: datetime
) -> dict[str, Any]:
    """Build the machine-readable convergence audit manifest. Pure given
    (conn, now) — issues no writes."""
    schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    classification = classify_convergence(conn)

    converged_ids = sorted(
        gid for gid, (status, _) in classification.items() if status == CONVERGED
    )
    missing_ids = sorted(
        gid for gid, (status, _) in classification.items() if status == MISSING_REVISION
    )
    divergent_ids = sorted(
        gid for gid, (status, _) in classification.items() if status == DIVERGENT_REVISION
    )
    mismatches = {
        str(gid): fields
        for gid, (status, fields) in sorted(classification.items())
        if status == DIVERGENT_REVISION
    }

    digest_payload = {
        "converged_grade_ids": converged_ids,
        "missing_revision_grade_ids": missing_ids,
        "divergent_revision_grade_ids": divergent_ids,
        "mismatches": mismatches,
        "db_schema_version": schema_version,
    }
    digest = hashlib.sha256(canonical_json(digest_payload).encode("utf-8")).hexdigest()

    return {
        "report_schema_version": CONVERGENCE_REPORT_SCHEMA_VERSION,
        "run_at": utc_canonical(now),
        "db_target": db_target,
        "db_schema_version": schema_version,
        "total_grades": len(classification),
        "converged_count": len(converged_ids),
        "missing_revision_count": len(missing_ids),
        "divergent_revision_count": len(divergent_ids),
        "converged_grade_ids": converged_ids,
        "missing_revision_grade_ids": missing_ids,
        "divergent_revision_grade_ids": divergent_ids,
        "mismatches": mismatches,
        "digest": digest,
    }


def run_convergence_audit(db_uri: str, *, now: datetime) -> dict[str, Any]:
    """Open db_uri read-only and build the convergence manifest. Raises
    AuditError if db_uri is not read-only."""
    conn = open_readonly_connection(db_uri)
    try:
        conn.execute("BEGIN")
        return build_convergence_manifest(conn, db_target=db_uri, now=now)
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def render_convergence_markdown(manifest: dict[str, Any]) -> str:
    """Render the comms report from the convergence manifest. No DB
    access — this is purely a rendering of the manifest."""
    lines = [
        "# Grade Revision Convergence Audit",
        "",
        f"- Run at: {manifest['run_at']}",
        f"- Database target: `{manifest['db_target']}`",
        f"- Database schema version: {manifest['db_schema_version']}",
        f"- Report schema version: {manifest['report_schema_version']}",
        f"- Total grades: {manifest['total_grades']}",
        f"- Converged: {manifest['converged_count']}",
        f"- Missing revision: {manifest['missing_revision_count']}",
        f"- Divergent revision: {manifest['divergent_revision_count']}",
        f"- Manifest digest: `{manifest['digest']}`",
        "",
        "## Missing revision",
        "",
        f"- Grade IDs: {manifest['missing_revision_grade_ids']}",
        "",
        "## Divergent revision",
        "",
        f"- Grade IDs: {manifest['divergent_revision_grade_ids']}",
        "",
    ]
    if manifest["mismatches"]:
        lines.append("Field-level mismatches (current row vs. latest revision):")
        lines.append("")
        for grade_id, fields in manifest["mismatches"].items():
            lines.append(f"- grade {grade_id}: {fields}")
        lines.append("")

    if "repair" in manifest:
        repair = manifest["repair"]
        lines.append("## Repair")
        lines.append("")
        if repair["dry_run"]:
            lines.append(f"- Dry run: would repair {repair['would_repair_count']} grade(s)")
            lines.append(f"- Would repair grade IDs: {repair['would_repair_grade_ids']}")
        else:
            lines.append(f"- Repaired: {repair['repaired_count']} grade(s)")
            lines.append(f"- Repaired grade IDs: {repair['repaired_grade_ids']}")
            lines.append(
                f"- Already converged before repair ran: {repair['already_converged_count']}"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def convergence_repair(
    db_path: str,
    manifest: dict[str, Any],
    *,
    apply: bool,
) -> dict[str, Any]:
    """Verify, and — only with ``apply=True`` — repair, every grade the
    manifest listed as missing_revision or divergent_revision.

    Unlike ``remediate`` above, this never applies operator-supplied
    replacement content, so there is no need to gate on a full-corpus
    digest match: the safety property comes from each grade's own
    check-under-lock inside
    ``StateManager.converge_grade_revision_for_remediation``, which
    re-reads the current row and latest revision fresh every time it
    runs. A grade the manifest listed as needing repair but that has
    since independently converged (e.g. a concurrent repair run, or a
    normal grade write appending its own revision) is reported as
    already converged rather than treated as an error — this makes a
    rerun with the same manifest safely idempotent without requiring a
    fresh audit first. ``--apply`` still requires the manifest's schema
    version to match the target database, and the database to already be
    at the latest schema, matching ``remediate``'s safety floor.
    """
    db_uri = Path(db_path).resolve().as_uri() + "?mode=rw"
    try:
        preflight = sqlite3.connect(db_uri, uri=True)
    except sqlite3.OperationalError as exc:
        raise AuditError(f"cannot open writable database {db_path!r}: {exc}") from exc
    try:
        db_schema_version = int(preflight.execute("PRAGMA user_version").fetchone()[0])
    finally:
        preflight.close()
    try:
        manifest_schema_version = int(manifest.get("db_schema_version", -1))
    except (TypeError, ValueError) as exc:
        raise AuditError(
            "convergence audit manifest db_schema_version must be an integer"
        ) from exc
    if db_schema_version != manifest_schema_version:
        raise AuditError(
            "convergence audit manifest schema version has drifted from the writable "
            f"database — expected {manifest_schema_version}, got {db_schema_version}"
        )
    if db_schema_version != LATEST_SCHEMA_VERSION:
        raise AuditError(
            "convergence repair requires the latest database schema before StateManager "
            f"opens it; upgrade to {LATEST_SCHEMA_VERSION}, then rerun the read-only "
            "convergence audit"
        )

    affected_grade_ids = sorted(
        set(manifest.get("missing_revision_grade_ids", []))
        | set(manifest.get("divergent_revision_grade_ids", []))
    )

    state = StateManager(db_path)
    try:
        if not apply:
            with state.db.read_transaction():
                would_repair: list[int] = []
                already_converged: list[int] = []
                for grade_id in affected_grade_ids:
                    row = state.conn.execute(
                        _GRADE_WITH_EDITED_TEXT_SELECT + " WHERE g.id = ?", (grade_id,)
                    ).fetchone()
                    if row is None:
                        raise AuditError(
                            "convergence audit manifest references unknown grade id "
                            f"{grade_id}"
                        )
                    current_shape = grade_revision_comparison_shape(row)
                    latest_payload = _latest_revision_payload(state.conn, grade_id)
                    status, _ = _classify_one_convergence(current_shape, latest_payload)
                    if status == CONVERGED:
                        already_converged.append(grade_id)
                    else:
                        would_repair.append(grade_id)
            return {
                "dry_run": True,
                "would_repair_count": len(would_repair),
                "would_repair_grade_ids": would_repair,
                "already_converged_count": len(already_converged),
                "already_converged_grade_ids": already_converged,
            }

        repaired: list[int] = []
        already_converged = []
        with state.db.begin_immediate():
            for grade_id in affected_grade_ids:
                status = state.converge_grade_revision_for_remediation(
                    grade_id,
                    remediation_reason=f"grade revision convergence repair {manifest['digest']}",
                )
                if status == CONVERGED:
                    already_converged.append(grade_id)
                else:
                    repaired.append(grade_id)
        return {
            "dry_run": False,
            "repaired_count": len(repaired),
            "repaired_grade_ids": repaired,
            "already_converged_count": len(already_converged),
            "already_converged_grade_ids": already_converged,
        }
    finally:
        state.close()


# --- CLI ---


def _cmd_audit(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    try:
        manifest = run_audit(args.db_uri, now=now)
    except AuditError as exc:
        print(f"grade-corpus audit error: {exc}", file=sys.stderr)
        return 2

    manifest_path = Path(args.manifest_output)
    if manifest_path.exists() and not args.replace:
        print(f"Refusing to replace existing manifest: {manifest_path}", file=sys.stderr)
        return 2
    manifest_path.write_text(canonical_json(manifest))

    report_path = Path(args.report_output)
    if report_path.exists() and not args.replace:
        print(f"Refusing to replace existing report: {report_path}", file=sys.stderr)
        return 2
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_markdown(manifest))

    print(f"Wrote manifest to {manifest_path} and report to {report_path}")
    print(f"Affected total: {manifest['affected_total']} / {manifest['total_eligible_grades']}")
    return 0


def _cmd_remediate(args: argparse.Namespace) -> int:
    try:
        manifest = json.loads(Path(args.manifest).read_text())
        replacement_manifest = None
        if args.replacement_manifest:
            replacement_manifest = json.loads(Path(args.replacement_manifest).read_text())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"grade-corpus remediate error: invalid manifest: {exc}", file=sys.stderr)
        return 2

    try:
        result = remediate(
            args.db_path,
            manifest,
            apply=args.apply,
            replacement_manifest=replacement_manifest,
            now=datetime.now(UTC),
        )
    except AuditError as exc:
        print(f"grade-corpus remediate error: {exc}", file=sys.stderr)
        return 2

    print(canonical_json(result), end="")
    return 0


def _cmd_convergence_audit(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    try:
        manifest = run_convergence_audit(args.db_uri, now=now)
    except AuditError as exc:
        print(f"grade-revision-convergence audit error: {exc}", file=sys.stderr)
        return 2

    manifest_path = Path(args.manifest_output)
    if manifest_path.exists() and not args.replace:
        print(f"Refusing to replace existing manifest: {manifest_path}", file=sys.stderr)
        return 2
    manifest_path.write_text(canonical_json(manifest))

    report_path = Path(args.report_output)
    if report_path.exists() and not args.replace:
        print(f"Refusing to replace existing report: {report_path}", file=sys.stderr)
        return 2
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_convergence_markdown(manifest))

    print(f"Wrote manifest to {manifest_path} and report to {report_path}")
    print(
        f"Converged: {manifest['converged_count']} / {manifest['total_grades']} — "
        f"missing_revision: {manifest['missing_revision_count']}, "
        f"divergent_revision: {manifest['divergent_revision_count']}"
    )
    return 0


def _cmd_convergence_repair(args: argparse.Namespace) -> int:
    try:
        manifest = json.loads(Path(args.manifest).read_text())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"grade-revision-convergence repair error: invalid manifest: {exc}", file=sys.stderr)
        return 2

    try:
        result = convergence_repair(
            args.db_path,
            manifest,
            apply=args.apply,
        )
    except AuditError as exc:
        print(f"grade-revision-convergence repair error: {exc}", file=sys.stderr)
        return 2

    print(canonical_json(result), end="")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grade_corpus_audit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit_p = subparsers.add_parser("audit", help="Run the read-only grade corpus audit")
    audit_p.add_argument(
        "--db-uri",
        required=True,
        help="Read-only SQLite URI; must include mode=ro",
    )
    audit_p.add_argument("--manifest-output", default="grade_corpus_audit_manifest.json")
    audit_p.add_argument("--report-output", default="grade_corpus_audit.md")
    audit_p.add_argument("--replace", action="store_true")
    audit_p.set_defaults(func=_cmd_audit)

    remediate_p = subparsers.add_parser("remediate", help="Flag/regrade known-bad rows")
    remediate_p.add_argument("--db-path", required=True, help="Writable database path")
    remediate_p.add_argument(
        "--manifest", required=True, help="Audit manifest JSON produced by `audit`"
    )
    remediate_p.add_argument("--replacement-manifest", help="Reviewed replacement manifest JSON")
    remediate_p.add_argument("--apply", action="store_true")
    remediate_p.set_defaults(func=_cmd_remediate)

    convergence_audit_p = subparsers.add_parser(
        "convergence-audit", help="Run the read-only grade_revisions convergence audit"
    )
    convergence_audit_p.add_argument(
        "--db-uri",
        required=True,
        help="Read-only SQLite URI; must include mode=ro",
    )
    convergence_audit_p.add_argument(
        "--manifest-output", default="grade_revision_convergence_manifest.json"
    )
    convergence_audit_p.add_argument(
        "--report-output", default="comms/grade-revision-convergence-audit.md"
    )
    convergence_audit_p.add_argument("--replace", action="store_true")
    convergence_audit_p.set_defaults(func=_cmd_convergence_audit)

    convergence_repair_p = subparsers.add_parser(
        "convergence-repair",
        help="Append missing/divergent grade_revisions entries to restore convergence",
    )
    convergence_repair_p.add_argument("--db-path", required=True, help="Writable database path")
    convergence_repair_p.add_argument(
        "--manifest",
        required=True,
        help="Convergence audit manifest JSON produced by `convergence-audit`",
    )
    convergence_repair_p.add_argument("--apply", action="store_true")
    convergence_repair_p.set_defaults(func=_cmd_convergence_repair)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
