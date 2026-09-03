"""Read-only, reproducible Phase 1 production-evidence audit.

The JSON object returned here is authoritative.  Markdown is deliberately a
rendering of that object, never a second set of database queries.

This module is the public compatibility facade. Snapshot loading lives in
``phase1_audit_data``, criterion evaluation in ``phase1_audit_criteria``,
and historical dossier replay in ``phase1_audit_replay``; ``run_audit``
orchestrates those seams and owns aggregation and report assembly. Every
symbol existing callers import (``run_audit``, ``canonical_json``,
``render_markdown``, ``AuditResult``, ``Criterion``, ``Finding``,
``REPORT_SCHEMA_VERSION``, ``parse_utc``, ``record_parent_change``) stays
importable from here.
"""
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scout.paa.audit.criteria import (
    Criterion as Criterion,
)
from scout.paa.audit.criteria import (
    Finding as Finding,
)
from scout.paa.audit.criteria import (
    active_project_set_criterion,
    attributable_deterministic_gate_block_criterion,
    complete_schema_v2_grades_criterion,
    evaluation_corpus_criterion,
    graded_bluesky_parent_context_change_criterion,
    production_live_scans_criterion,
    surfaced_pairing_and_rate_criterion,
    window_criterion,
)
from scout.paa.audit.data import load_snapshot, open_read_only_connection
from scout.paa.audit.data import parse_utc as parse_utc
from scout.paa.audit.replay import (
    DossierCache,
    evaluate_dossier_readiness_and_approval,
    evaluate_historical_content_replay,
)

REPORT_SCHEMA_VERSION = 4


def canonical_json(value: object) -> str:
    """Stable JSON encoding used for reports and bundle artifacts."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


@dataclass(frozen=True)
class AuditResult:
    report: dict[str, Any]
    passed: bool


def run_audit(
    db_path: Path,
    dossier_root: Path,
    *,
    window_from: datetime,
    window_to: datetime,
    strict: bool = False,
    approval_references: dict[str, dict[str, str]] | None = None,
    corpus_dir: Path | None = None,
) -> AuditResult:
    """Return a complete report without modifying SQLite or the dossier checkout."""
    if window_from.tzinfo is None or window_to.tzinfo is None:
        raise ValueError("audit boundaries must include UTC offsets")
    window_from, window_to = window_from.astimezone(UTC), window_to.astimezone(UTC)
    if window_from >= window_to:
        raise ValueError("--from must be earlier than --to")
    if strict and window_to - window_from < timedelta(days=14):
        raise ValueError("strict evidence requires a contiguous window of at least 14 days")

    with open_read_only_connection(db_path) as conn:
        snapshot = load_snapshot(conn, window_from, window_to)

    dossier_cache: DossierCache = {}
    dossier_rows, readiness, approval = evaluate_dossier_readiness_and_approval(
        snapshot, dossier_root, approval_references, dossier_cache
    )
    gate_block_criterion, qualified_blocks = attributable_deterministic_gate_block_criterion(
        snapshot
    )
    parent_context_criterion, changes = graded_bluesky_parent_context_change_criterion(snapshot)
    replay_rows, replay_criterion = evaluate_historical_content_replay(
        snapshot, dossier_root, dossier_cache
    )
    lint_corpus_dir = corpus_dir or Path(__file__).parents[2] / "evals" / "phase1"
    evaluation_corpus, lint = evaluation_corpus_criterion(lint_corpus_dir, dossier_root)

    criteria: dict[str, Criterion] = {
        "window_at_least_14_days": window_criterion(window_from, window_to),
        "production_live_scans_present": production_live_scans_criterion(snapshot),
        "active_project_set_exact": active_project_set_criterion(snapshot),
        "historical_dossier_readiness": readiness,
        "external_approval_exact_revision": approval,
        "complete_schema_v2_grades": complete_schema_v2_grades_criterion(snapshot),
        "attributable_deterministic_gate_block": gate_block_criterion,
        "graded_bluesky_parent_context_change": parent_context_criterion,
        "surfaced_pairing_and_rate": surfaced_pairing_and_rate_criterion(snapshot),
        "historical_content_replay": replay_criterion,
        "evaluation_corpus": evaluation_corpus,
    }

    parent_statuses = Counter(
        str(e.get("parent_lookup_status") or "missing") for e in snapshot.evaluations
    )
    reply_rows = [
        e
        for e in snapshot.evaluations
        if e.get("platform") == "bluesky"
        and e.get("parent_lookup_status") in {"resolved", "failed"}
    ]
    resolved = [e for e in reply_rows if e.get("parent_lookup_status") == "resolved"]

    start, end = window_from.isoformat(), window_to.isoformat()
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "window": {"from": start, "to": end, "interval": "[from,to)"},
        "strict": strict,
        "registry": {"active_projects": list(snapshot.active_projects)},
        "criteria": {key: value.as_dict() for key, value in criteria.items()},
        "counts": {
            "scans_total": len(snapshot.scans),
            "production_live_scans": len(snapshot.qualifying_scans),
            "excluded_scans": dict(sorted(snapshot.excluded_scans.items())),
            "evaluations": len(snapshot.evaluations),
        },
        "scans": snapshot.qualifying_scans,
        "dossiers": dossier_rows,
        "grading": {
            "evaluation_count": len(snapshot.evaluations),
            "grade_count": len(snapshot.grades),
            "orphan_grade_ids": [g["id"] for g in snapshot.potential_orphans],
        },
        "gate_blocks": {"all": snapshot.blocks, "qualifying": qualified_blocks},
        "parent_context": {
            "lookup_status_counts": dict(sorted(parent_statuses.items())),
            "reply_denominator": len(reply_rows),
            "resolved_parent_numerator": len(resolved),
            "resolved_parent_rate": len(resolved) / len(reply_rows) if reply_rows else None,
            "fetch_failure_count": len(snapshot.fetch_failures),
            "fetch_failure_reasons": dict(
                Counter(str(row.get("kind") or "unknown") for row in snapshot.fetch_failures)
            ),
            "assessments": snapshot.assessments,
            "outcome_changes": changes,
        },
        "surfaced_events": {"count": len(snapshot.events), "events": snapshot.events},
        "historical_replay": replay_rows,
        "corpus_lint": lint,
    }
    passed = all(value.passed for value in criteria.values())
    return AuditResult(report=report, passed=passed)


def render_markdown(report: dict[str, Any]) -> str:
    """Render Markdown exclusively from an already-built report object."""
    lines = [
        "# Scout Phase 1 exit evidence",
        "",
        f"Schema: `{report['schema_version']}`",
        f"Window: `{report['window']['from']}` to `{report['window']['to']}` ({report['window']['interval']})",
        "",
        "## Criteria",
        "",
    ]
    for criterion_id, criterion in report["criteria"].items():
        lines.append(
            f"- [{'x' if criterion['passed'] else ' '}] `{criterion_id}` ({criterion['numerator']}/{criterion['denominator']})"
        )
        for finding in criterion["findings"]:
            record = f" record `{finding['record_id']}`" if finding["record_id"] is not None else ""
            gate = f" gate `{finding['gate_code']}`" if finding.get("gate_code") else ""
            lines.append(f"  -{record}{gate}: {finding['reason']}")
    counts = report["counts"]
    lines.extend(
        [
            "",
            "## Population",
            "",
            f"- Production/live scans: {counts['production_live_scans']}",
            f"- Evaluations: {counts['evaluations']}",
            f"- Excluded scans: {counts['excluded_scans']}",
            "",
        ]
    )
    return "\n".join(lines)


def record_parent_change(
    db_path: Path,
    *,
    evaluation_id: int,
    without_parent_relevance: str,
    without_parent_posture: str,
    assessor: str,
    explanation: str,
) -> int:
    """Record an attributable human counterfactual for a real graded reply."""
    if without_parent_relevance not in {"relevant", "not_relevant"}:
        raise ValueError("without-parent relevance must be relevant or not_relevant")
    if without_parent_posture not in {"answer", "engage", "ask", "abstain"}:
        raise ValueError("invalid without-parent posture")
    if not assessor.strip() or not explanation.strip():
        raise ValueError("assessor and explanation are required")
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT e.id, p.platform, p.parent_lookup_status, g.id AS grade_id, g.schema_version, g.needs_regrade FROM evaluations e JOIN posts p ON p.id=e.post_id LEFT JOIN grades g ON g.evaluation_id=e.id WHERE e.id=?",
            (evaluation_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"evaluation {evaluation_id} does not exist")
        if row["platform"] != "bluesky" or row["parent_lookup_status"] != "resolved":
            raise ValueError("evaluation must be a Bluesky post with a resolved parent")
        if row["grade_id"] is None or row["schema_version"] != 2 or row["needs_regrade"]:
            raise ValueError("evaluation must have a complete schema-v2 grade")
        cursor = conn.execute(
            """INSERT INTO parent_context_assessments (evaluation_id, assessor, assessed_at, without_parent_relevance, without_parent_posture, explanation) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(evaluation_id) DO UPDATE SET assessor=excluded.assessor, assessed_at=excluded.assessed_at, without_parent_relevance=excluded.without_parent_relevance, without_parent_posture=excluded.without_parent_posture, explanation=excluded.explanation""",
            (
                evaluation_id,
                assessor.strip(),
                datetime.now(UTC).isoformat(),
                without_parent_relevance,
                without_parent_posture,
                explanation.strip(),
            ),
        )
        conn.commit()
        return int(
            cursor.lastrowid
            or conn.execute(
                "SELECT id FROM parent_context_assessments WHERE evaluation_id=?", (evaluation_id,)
            ).fetchone()[0]
        )
    finally:
        conn.close()
