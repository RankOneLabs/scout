"""Phase 1 audit criterion evaluation.

Each function here is a pure judgment over an ``AuditSnapshot`` (or, for
``evaluation_corpus``, the corpus and dossier paths) producing one
``Criterion``. Historical dossier readiness, external approval, and content
replay are evaluated in ``phase1_audit_replay`` instead — they need a git
checkout and a resolution cache, not just the snapshot.
"""
# ruff: noqa: E501

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scout.paa.audit.data import AuditSnapshot, complete_grade, parse_utc

DETERMINISTIC_GATE_CODES = frozenset(
    {
        "fact_ids",
        "safe_phrasing",
        "resource_ids",
        "url_allowlist",
        "prohibitions",
        "platform_limits",
        "structure_projections",
        "posture",
    }
)


@dataclass(frozen=True)
class Finding:
    criterion_id: str
    record_id: str | int | None
    reason: str
    gate_code: str | None = None
    dossier_revision: str | None = None
    summary_id: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class Criterion:
    passed: bool
    numerator: int | None
    denominator: int | None
    evidence_refs: tuple[str, ...]
    findings: tuple[Finding, ...]

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence_refs"] = list(self.evidence_refs)
        result["findings"] = [asdict(item) for item in self.findings]
        return result


def build_criterion(
    passed: bool,
    numerator: int | None,
    denominator: int | None,
    refs: tuple[str, ...],
    findings: list[Finding],
) -> Criterion:
    return Criterion(passed, numerator, denominator, refs, tuple(findings))


def window_criterion(window_from: datetime, window_to: datetime) -> Criterion:
    findings: list[Finding] = []
    if window_to - window_from < timedelta(days=14):
        findings.append(
            Finding(
                "window_at_least_14_days",
                None,
                "window is shorter than 14 contiguous 24-hour days",
            )
        )
    return build_criterion(
        window_to - window_from >= timedelta(days=14),
        int((window_to - window_from).total_seconds()),
        int(timedelta(days=14).total_seconds()),
        ("window",),
        findings,
    )


def production_live_scans_criterion(snapshot: AuditSnapshot) -> Criterion:
    qualifying_scans = snapshot.qualifying_scans
    return build_criterion(
        bool(qualifying_scans),
        len(qualifying_scans),
        len(snapshot.scans),
        tuple(f"scan:{s['id']}" for s in qualifying_scans),
        []
        if qualifying_scans
        else [Finding("production_live_scans_present", None, "no production/live scan in window")],
    )


def active_project_set_criterion(snapshot: AuditSnapshot) -> Criterion:
    """A qualifying scan must have observed exactly the projects the
    read-only SQLite registry currently marks active. The registry is the
    sole project authority; the audit never carries a second hardcoded
    project policy."""
    expected_projects = frozenset(snapshot.active_projects)
    scan_ids = [int(s["id"]) for s in snapshot.qualifying_scans]
    project_findings: list[Finding] = []
    projects_per_scan: dict[int, list[str]] = {}
    for scan_id in scan_ids:
        observed = sorted(
            {
                str(e["project_key"])
                for e in snapshot.evaluations
                if e["scan_id"] == scan_id and e.get("project_key")
            }
        )
        projects_per_scan[scan_id] = observed
        if set(observed) != expected_projects:
            project_findings.append(
                Finding(
                    "active_project_set_exact",
                    scan_id,
                    f"observed active projects {observed}; expected {list(snapshot.active_projects)}",
                )
            )
    return build_criterion(
        bool(scan_ids) and not project_findings,
        len(scan_ids) - len(project_findings),
        len(scan_ids),
        tuple(f"scan:{key}" for key in projects_per_scan),
        project_findings,
    )


def complete_schema_v2_grades_criterion(snapshot: AuditSnapshot) -> Criterion:
    evaluations = snapshot.evaluations
    grade_findings: list[Finding] = []
    for evaluation in evaluations:
        owned = snapshot.grades_by_eval.get(int(evaluation["id"]), [])
        if len(owned) != 1:
            grade_findings.append(
                Finding(
                    "complete_schema_v2_grades",
                    evaluation["id"],
                    f"expected exactly one grade, found {len(owned)}",
                )
            )
        elif not complete_grade(owned[0]):
            grade_findings.append(
                Finding(
                    "complete_schema_v2_grades",
                    evaluation["id"],
                    "grade is missing required schema-v2 causal fields",
                )
            )
    for grade in snapshot.potential_orphans:
        grade_findings.append(
            Finding(
                "complete_schema_v2_grades",
                grade["id"],
                "orphan grade in production/live population",
            )
        )
    return build_criterion(
        bool(evaluations) and not grade_findings,
        len(evaluations)
        - sum(
            1
            for f in grade_findings
            if isinstance(f.record_id, int) and f.record_id in snapshot.evaluation_ids
        ),
        len(evaluations),
        tuple(f"evaluation:{e['id']}" for e in evaluations),
        grade_findings,
    )


def attributable_deterministic_gate_block_criterion(
    snapshot: AuditSnapshot,
) -> tuple[Criterion, list[dict[str, Any]]]:
    blocks = snapshot.blocks
    block_findings: list[Finding] = []
    qualified_blocks: list[dict[str, Any]] = []
    for block in blocks:
        problems: list[str] = []
        if block.get("surface_status") != "gate_blocked":
            problems.append("evaluation is not gate_blocked")
        if block.get("reason_code") not in DETERMINISTIC_GATE_CODES:
            problems.append("gate code is not deterministic")
        if not str(block.get("offending_text") or "").strip():
            problems.append("offending text is blank")
        if (
            not str(block.get("platform_msg_id") or "").strip()
            or not str(block.get("content") or "").strip()
        ):
            problems.append("source document identity is absent")
        if not str(block.get("author_id") or "").strip():
            problems.append("source author identity is absent")
        if problems:
            block_findings.append(
                Finding(
                    "attributable_deterministic_gate_block",
                    block["id"],
                    "; ".join(problems),
                    gate_code=block.get("reason_code"),
                    dossier_revision=block.get("dossier_revision"),
                    summary_id=block.get("dossier_summary_id"),
                )
            )
        else:
            qualified_blocks.append(block)
    if not qualified_blocks:
        block_findings.append(
            Finding(
                "attributable_deterministic_gate_block",
                None,
                "no qualifying production/live deterministic gate block",
            )
        )
    criterion = build_criterion(
        bool(qualified_blocks),
        len(qualified_blocks),
        max(1, len(blocks)),
        tuple(f"gate_block:{b['id']}" for b in qualified_blocks),
        block_findings,
    )
    return criterion, qualified_blocks


def graded_bluesky_parent_context_change_criterion(
    snapshot: AuditSnapshot,
) -> tuple[Criterion, list[dict[str, Any]]]:
    assessments = snapshot.assessments
    grades_by_eval = snapshot.grades_by_eval
    changes = [
        a
        for a in assessments
        if a["platform"] == "bluesky"
        and a["parent_lookup_status"] == "resolved"
        and a.get("parent_id")
        and (
            a["without_parent_posture"] != a["posture"]
            or (a["without_parent_relevance"] == "relevant") != bool(a["relevant"])
        )
        and len(grades_by_eval.get(int(a["evaluation_id"]), [])) == 1
        and complete_grade(grades_by_eval[int(a["evaluation_id"])][0])
    ]
    parent_findings = (
        []
        if changes
        else [
            Finding(
                "graded_bluesky_parent_context_change",
                None,
                "no graded attributable Bluesky reply proves parent context changed relevance or posture",
            )
        ]
    )
    criterion = build_criterion(
        bool(changes),
        len(changes),
        1,
        tuple(f"assessment:{a['id']}" for a in changes),
        parent_findings,
    )
    return criterion, changes


def surfaced_pairing_and_rate_criterion(snapshot: AuditSnapshot) -> Criterion:
    evaluations = snapshot.evaluations
    drafts_by_eval = snapshot.drafts_by_eval
    events_by_eval = snapshot.events_by_eval
    events = snapshot.events
    pairing_findings: list[Finding] = []
    violating_event_ids: set[int] = set()
    for evaluation in evaluations:
        eid, surfaced = int(evaluation["id"]), evaluation.get("surface_status") == "surfaced"
        ds, es = drafts_by_eval[eid], events_by_eval[eid]
        if surfaced and (len(ds) != 1 or len(es) != 1):
            violating_event_ids.update(int(event["id"]) for event in es)
            pairing_findings.append(
                Finding(
                    "surfaced_pairing_and_rate",
                    eid,
                    f"surfaced evaluation requires one draft and one event; found {len(ds)} drafts and {len(es)} events",
                )
            )
        if not surfaced and es:
            violating_event_ids.update(int(event["id"]) for event in es)
            pairing_findings.append(
                Finding(
                    "surfaced_pairing_and_rate",
                    eid,
                    "non-surfaced evaluation has surfaced event",
                )
            )
        for event in es:
            if not str(event.get("author_id") or "").strip():
                violating_event_ids.add(int(event["id"]))
                pairing_findings.append(
                    Finding(
                        "surfaced_pairing_and_rate",
                        event["id"],
                        "event author identity is blank",
                    )
                )
            if (
                event.get("post_id") != evaluation["post_id"]
                or len(ds) != 1
                or event.get("draft_id") != ds[0]["id"]
            ):
                violating_event_ids.add(int(event["id"]))
                pairing_findings.append(
                    Finding(
                        "surfaced_pairing_and_rate",
                        event["id"],
                        "event foreign keys do not match its evaluation and draft",
                    )
                )
    prior_by_author: dict[str, dict[str, Any]] = {}
    for event in events:
        author = str(event.get("author_id") or "").strip()
        if not author:
            continue
        prior = prior_by_author.get(author)
        if prior and parse_utc(str(event["surfaced_at"])) - parse_utc(
            str(prior["surfaced_at"])
        ) < timedelta(days=7):
            violating_event_ids.add(int(event["id"]))
            pairing_findings.append(
                Finding(
                    "surfaced_pairing_and_rate",
                    event["id"],
                    f"within seven days of surfaced event {prior['id']}",
                )
            )
        prior_by_author[author] = event
    return build_criterion(
        not pairing_findings,
        len(events) - len(violating_event_ids),
        len(events),
        tuple(f"event:{event['id']}" for event in events),
        pairing_findings,
    )


def _lint_report(corpus_dir: Path, dossier_root: Path) -> dict[str, Any]:
    """Use the corpus linter's structured seam without parsing console prose."""
    from scripts.lint_eval_corpus import lint_report

    return dict(lint_report(corpus_dir, dossier_root))


def evaluation_corpus_criterion(
    corpus_dir: Path, dossier_root: Path
) -> tuple[Criterion, dict[str, Any]]:
    lint = _lint_report(corpus_dir, dossier_root)
    lint_findings = [Finding("evaluation_corpus", None, message) for message in lint["errors"]]
    criterion = build_criterion(
        bool(lint["ok"]),
        int(lint["case_count"]),
        int(lint["minimum_cases"]),
        tuple(f"corpus:{case_id}" for case_id in lint.get("case_ids", [])),
        lint_findings,
    )
    return criterion, lint
