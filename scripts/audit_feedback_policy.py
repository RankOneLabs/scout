"""Read-only reconciliation of evaluation-feedback/v1 selection against the
legacy `get_recent_grading_signals(limit_scans=3)` selection it replaces.

Never mutates: requires a SQLite URI containing `mode=ro`, additionally
sets `PRAGMA query_only=ON` — mechanically incapable of writing regardless
of transaction state — and wraps its multiple read statements (the
population load and the separate legacy count) in one explicit
BEGIN/ROLLBACK transaction so both see the same consistent snapshot. It
has no StateManager connection and cannot route through `Db` (db.py has no
`mode=ro`/URI connection mode), so it is the sole owner of
transaction mechanics for its own private, read-only connection; see
`tests/test_transaction_source_guard.py`. Runs the same pure
`evaluation_feedback` pipeline functions used in production
(`load_grade_population`, `classify_feedback_eligibility`) against that
connection, and separately counts what the legacy scan-scoped query would
have selected, so the two counts are directly comparable over the
identical corpus.

The legacy query selects grades by originating scan (the most recently
*completed* three scans) — it can miss a human judgment entered today on
an evaluation from an old scan. evaluation-feedback/v1 selects by
graded_at recency instead. This script quantifies that gap: it reports
every in-window grade's disposition (eligible, eligible_cap, or one of the
six exclusion reasons), not just the two totals, so an operator can see
exactly which rows the legacy query dropped and why.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scout.config import HUMAN_GRADE_SCHEMA_VERSION
from scout.grading.feedback import (
    ALL_EXCLUSION_REASONS,
    FeedbackPolicyConfig,
    classify_feedback_eligibility,
    load_grade_population,
    resolve_feedback_policy_config,
)
from scout.storage.state import LATEST_SCHEMA_VERSION

REPORT_SCHEMA_VERSION = 1

LEGACY_LIMIT_SCANS = 3

_LEGACY_ELIGIBLE_SQL = (
    "SELECT COUNT(*) FROM grades g "
    "JOIN evaluations e ON e.id = g.evaluation_id "
    f"WHERE g.schema_version = {HUMAN_GRADE_SCHEMA_VERSION} AND g.needs_regrade = 0 "
    "AND e.scan_id IN ("
    "  SELECT id FROM scans WHERE completed_at IS NOT NULL "
    "  ORDER BY id DESC LIMIT ?"
    ")"
)


class AuditError(Exception):
    """Raised for CLI-facing audit usage errors."""


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

    Raises AuditError if the URI does not request read-only mode, or if
    the schema predates the evaluation-feedback/v1 migration (27) — a
    read-only connection cannot run migrations, so an older schema means
    the operator must upgrade the target database (or point at a copy
    that already has) before auditing it.
    """
    if "mode=ro" not in db_uri:
        raise AuditError(f"db_uri must include mode=ro, got {db_uri!r}")
    conn = sqlite3.connect(db_uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if schema_version < 27:
        conn.close()
        raise AuditError(
            f"database schema version {schema_version} predates "
            f"evaluation-feedback/v1 (migration 27); upgrade to "
            f"{LATEST_SCHEMA_VERSION} before auditing"
        )
    return conn


def legacy_prompt_eligible_count(
    conn: sqlite3.Connection, *, limit_scans: int = LEGACY_LIMIT_SCANS
) -> int:
    """Count of grades the legacy get_recent_grading_signals(limit_scans)
    query would select: schema_version=HUMAN_GRADE_SCHEMA_VERSION,
    needs_regrade=0, and linked to an evaluation whose scan is among the
    `limit_scans` most recently *completed* scans — selection gated by
    originating scan, not by human grading recency."""
    row = conn.execute(_LEGACY_ELIGIBLE_SQL, (limit_scans,)).fetchone()
    return int(row[0])


def build_manifest(
    conn: sqlite3.Connection,
    *,
    db_target: str,
    now: datetime,
    config: FeedbackPolicyConfig,
) -> dict[str, Any]:
    """Build the machine-readable reconciliation manifest. Pure given
    (conn, now, config) — issues no writes.

    `conn` is opened autocommit (see `open_readonly_connection`), and this
    function issues more than one read statement (the population load and
    the separate legacy count), so it wraps them in an explicit
    transaction to pin one consistent snapshot across all of them — without
    it, a concurrent write to the target database between statements could
    make the two counts describe different moments.
    """
    conn.execute("BEGIN")
    try:
        schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])

        population = load_grade_population(conn, as_of=now, config=config)
        eligibility = classify_feedback_eligibility(population, config=config)

        disposition_counts: Counter[str] = Counter()
        for result in eligibility:
            disposition_counts[result.reason or "eligible"] += 1

        new_considered_valid_count = disposition_counts["eligible"]
        legacy_count = legacy_prompt_eligible_count(conn, limit_scans=LEGACY_LIMIT_SCANS)
    finally:
        conn.execute("ROLLBACK")

    dispositions_report = {
        reason: disposition_counts.get(reason, 0)
        for reason in ("eligible", *ALL_EXCLUSION_REASONS)
    }

    excluded_grade_ids = {
        reason: [r.row.grade_id for r in eligibility if r.reason == reason]
        for reason in ALL_EXCLUSION_REASONS
        if any(r.reason == reason for r in eligibility)
    }

    payload = {
        "policy_version": config.policy_version,
        "lookback_days": config.lookback_days,
        "max_grades": config.max_grades,
        "population_count": len(population),
        "new_considered_valid_count": new_considered_valid_count,
        "legacy_prompt_eligible_count": legacy_count,
        "legacy_limit_scans": LEGACY_LIMIT_SCANS,
        "dispositions": dispositions_report,
        "db_schema_version": schema_version,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "run_at": utc_canonical(now),
        "db_target": db_target,
        **payload,
        "excluded_grade_ids": excluded_grade_ids,
        "digest": digest,
    }


def run_audit(db_uri: str, *, now: datetime, config: FeedbackPolicyConfig) -> dict[str, Any]:
    """Open db_uri read-only and build the manifest. Raises AuditError if
    db_uri is not read-only or the schema is too old."""
    conn = open_readonly_connection(db_uri)
    try:
        return build_manifest(conn, db_target=db_uri, now=now, config=config)
    finally:
        conn.close()


def render_markdown(manifest: dict[str, Any]) -> str:
    """Render the comms report from the manifest object. No DB access —
    this is purely a rendering of the manifest."""
    lines = [
        "# Evaluation Feedback Policy Verification",
        "",
        f"- Run at: {manifest['run_at']}",
        f"- Database target: `{manifest['db_target']}`",
        f"- Database schema version: {manifest['db_schema_version']}",
        f"- Policy version: {manifest['policy_version']}",
        f"- Lookback days: {manifest['lookback_days']}",
        f"- Max grades: {manifest['max_grades']}",
        "",
        "## Reconciliation",
        "",
        f"- In-window population: {manifest['population_count']}",
        f"- **New considered-valid count (evaluation-feedback/v1):** "
        f"{manifest['new_considered_valid_count']}",
        f"- **Legacy prompt-eligible count "
        f"(get_recent_grading_signals, limit_scans={manifest['legacy_limit_scans']}):** "
        f"{manifest['legacy_prompt_eligible_count']}",
        "",
        "## Dispositions",
        "",
        "| Disposition | Count |",
        "| --- | --- |",
    ]
    for disposition, count in manifest["dispositions"].items():
        lines.append(f"| {disposition} | {count} |")

    if manifest["excluded_grade_ids"]:
        lines += ["", "## Excluded grade ids by reason", ""]
        for reason, grade_ids in manifest["excluded_grade_ids"].items():
            lines.append(f"- `{reason}` ({len(grade_ids)}): {grade_ids}")

    lines += ["", f"Digest: `{manifest['digest']}`", ""]
    return "\n".join(lines)


# --- CLI ---


def _cmd_audit(args: argparse.Namespace) -> int:
    now = (
        datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        if args.as_of
        else datetime.now(UTC)
    )
    config = resolve_feedback_policy_config()
    if args.lookback_days is not None:
        config = replace(config, lookback_days=args.lookback_days)
    if args.max_grades is not None:
        config = replace(config, max_grades=args.max_grades)

    try:
        manifest = run_audit(args.db_uri, now=now, config=config)
    except AuditError as exc:
        print(f"feedback-policy audit error: {exc}", file=sys.stderr)
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
    print(
        f"New considered-valid: {manifest['new_considered_valid_count']} / "
        f"Legacy prompt-eligible: {manifest['legacy_prompt_eligible_count']}"
    )
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audit_feedback_policy")
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit_p = subparsers.add_parser(
        "audit", help="Run the read-only evaluation-feedback/v1 reconciliation audit"
    )
    audit_p.add_argument(
        "--db-uri",
        required=True,
        help="Read-only SQLite URI; must include mode=ro",
    )
    audit_p.add_argument(
        "--as-of", default=None,
        help="ISO-8601 UTC instant to audit as-of (default: now); "
        "use to reproduce a prior run deterministically",
    )
    audit_p.add_argument("--lookback-days", type=int, default=None)
    audit_p.add_argument("--max-grades", type=int, default=None)
    audit_p.add_argument(
        "--manifest-output", default="feedback_policy_audit_manifest.json"
    )
    audit_p.add_argument(
        "--report-output", default="feedback_policy_audit.md"
    )
    audit_p.add_argument("--replace", action="store_true")
    audit_p.set_defaults(func=_cmd_audit)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
