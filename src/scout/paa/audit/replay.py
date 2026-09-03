"""Historical dossier replay: readiness, approval, and content-gate replay
against the exact pinned revisions production evidence recorded.

Dossier resolution and publishable-text verification stay on historical
pinned revisions rather than the synthetic Phase 1 execution adapter, so the
audit replays the evidence that was actually pinned. Every dossier resolved
here goes through one per-run cache shared between readiness/approval
evaluation and content replay, so a revision pinned by both an evaluation
and its surfaced draft is only resolved once.
"""
# ruff: noqa: E501

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from scout.config import SCOUT_DOSSIER_MAX_AGE_DAYS, SCOUT_DOSSIER_MIN_ENTRIES
from scout.dossiers.resolver import DossierResolution, DossierResolutionError, resolve_dossier
from scout.paa.audit.criteria import Criterion, Finding, build_criterion
from scout.paa.audit.data import AuditSnapshot, complete_grade
from scout.scanning.schemas import StructuredDraftOutput
from scout.verifier import verify_draft_content

DossierCache = dict[tuple[str, str, str], DossierResolution]


def _historical_dossier(
    root: Path, project_key: str, summary_id: str, revision: str
) -> DossierResolution:
    """Resolve an exact immutable dossier through the canonical resolver.

    Project identity comes from the caller (the evaluation or draft row)
    because resolve_dossier already owns immutable index lookup and
    validation — this function no longer hand-parses index.yaml.
    """
    try:
        return resolve_dossier(
            root,
            revision,
            project_key,
            summary_id,
            max_age_days=SCOUT_DOSSIER_MAX_AGE_DAYS,
            min_entries=SCOUT_DOSSIER_MIN_ENTRIES,
        )
    except DossierResolutionError as exc:
        raise ValueError(str(exc)) from exc


def _approval_for(
    approvals: dict[str, dict[str, str]] | None, project_key: str, revision: str | None
) -> dict[str, str] | None:
    record = (approvals or {}).get(project_key)
    if not isinstance(record, dict) or not revision:
        return None
    if record.get("revision") != revision or not str(record.get("reference") or "").strip():
        return None
    return {"revision": revision, "reference": str(record["reference"])}


def evaluate_dossier_readiness_and_approval(
    snapshot: AuditSnapshot,
    dossier_root: Path,
    approval_references: dict[str, dict[str, str]] | None,
    cache: DossierCache,
) -> tuple[list[dict[str, Any]], Criterion, Criterion]:
    """Resolve every revision production actually pinned, and record reviewer metadata.

    The registry is the sole project authority; this never carries a second
    hardcoded project policy.
    """
    dossier_observed: dict[tuple[str, str, str], dict[str, Any]] = {}
    dossier_findings: list[Finding] = []
    approval_findings: list[Finding] = []
    for e in snapshot.evaluations:
        project, summary, revision = (
            str(e.get("project_key") or ""),
            str(e.get("dossier_summary_id") or ""),
            str(e.get("dossier_revision") or ""),
        )
        key = (project, summary, revision)
        if key in dossier_observed:
            continue
        row: dict[str, Any] = {
            "project_key": project,
            "summary_id": summary,
            "revision": revision,
            "ready": False,
        }
        try:
            if not all(key):
                raise ValueError("evaluation has no project, summary id, or pinned revision")
            if key not in cache:
                cache[key] = _historical_dossier(dossier_root, project, summary, revision)
            resolution = cache[key]
            row.update(
                {
                    "ready": True,
                    "path": resolution.metadata.path,
                    "reviewer": resolution.summary.reviewer,
                    "last_reviewed": str(resolution.summary.last_reviewed),
                    "fact_count": len(resolution.summary.facts),
                    "resource_count": len(resolution.summary.resources),
                    "prohibition_count": len(resolution.summary.prohibitions),
                    "gap_count": len(resolution.known_gaps),
                }
            )
        except (ValueError, ValidationError) as exc:
            row["error"] = str(exc)
            dossier_findings.append(
                Finding(
                    "historical_dossier_readiness",
                    e["id"],
                    str(exc),
                    dossier_revision=revision or None,
                    summary_id=summary or None,
                )
            )
        approval = _approval_for(approval_references, project, revision)
        row["approval"] = approval
        if approval is None:
            approval_findings.append(
                Finding(
                    "external_approval_exact_revision",
                    e["id"],
                    "missing external approval reference for exact pinned revision",
                    dossier_revision=revision or None,
                    summary_id=summary or None,
                )
            )
        dossier_observed[key] = row
    dossier_rows = sorted(
        dossier_observed.values(),
        key=lambda row: (row["project_key"], row["revision"], row["summary_id"]),
    )
    readiness = build_criterion(
        bool(dossier_rows) and not dossier_findings,
        len(dossier_rows) - len(dossier_findings),
        len(dossier_rows),
        tuple(f"dossier:{r['summary_id']}@{r['revision']}" for r in dossier_rows),
        dossier_findings,
    )
    approval_criterion = build_criterion(
        bool(dossier_rows) and not approval_findings,
        len(dossier_rows) - len(approval_findings),
        len(dossier_rows),
        tuple(
            f"approval:{r['project_key']}@{r['revision']}"
            for r in dossier_rows
            if r.get("approval")
        ),
        approval_findings,
    )
    return dossier_rows, readiness, approval_criterion


def evaluate_historical_content_replay(
    snapshot: AuditSnapshot,
    dossier_root: Path,
    cache: DossierCache,
) -> tuple[list[dict[str, Any]], Criterion]:
    """Replay all surfaced drafts plus every accepted inbound draft.

    Inputs stay in the denominator even if their historical revision is
    corrupt.
    """
    replay_drafts: dict[int, dict[str, Any]] = {}
    for draft in snapshot.drafts:
        grade_rows = snapshot.grades_by_eval.get(int(draft["evaluation_id"]), [])
        accepted = (
            len(grade_rows) == 1
            and complete_grade(grade_rows[0])
            and grade_rows[0].get("action_judgment") == "accept"
        )
        if draft.get("surface_status") == "surfaced" or accepted:
            replay_drafts[int(draft["id"])] = draft
    replay_findings: list[Finding] = []
    replay_rows: list[dict[str, Any]] = []
    for draft in replay_drafts.values():
        project, revision, summary = (
            str(draft.get("project_key") or ""),
            str(draft.get("dossier_revision") or ""),
            str(draft.get("dossier_summary_id") or ""),
        )
        base = {
            "draft_id": draft["id"],
            "evaluation_id": draft["evaluation_id"],
            "project_key": project,
            "dossier_revision": revision,
            "summary_id": summary,
        }
        try:
            if not project or not revision or not summary:
                raise ValueError(
                    "draft is missing pinned project, dossier revision, or summary id"
                )
            replay_key = (project, summary, revision)
            if replay_key not in cache:
                cache[replay_key] = _historical_dossier(dossier_root, project, summary, revision)
            resolution = cache[replay_key]
            structured = StructuredDraftOutput.model_validate_json(
                draft.get("structured_output") or ""
            )
            checked = verify_draft_content(
                dossier=resolution.summary,
                structured_draft=structured,
                platform=str(draft.get("platform") or ""),
                author_id=str(draft.get("author_id") or ""),
            )
            reasons = [v.reason_code for v in checked.violations]
            if checked.assembled_text != draft.get("comment_text"):
                reasons.append("structured_text_mismatch")
            path = resolution.metadata.path
            base.update({"path": path, "gate_codes": reasons, "passed": not reasons})
            for reason in reasons:
                replay_findings.append(
                    Finding(
                        "historical_content_replay",
                        draft["id"],
                        "historical content gate failed",
                        gate_code=reason,
                        dossier_revision=revision,
                        summary_id=summary,
                        path=path,
                    )
                )
        except (ValueError, ValidationError) as exc:
            base.update(
                {"passed": False, "gate_codes": ["historical_input_error"], "error": str(exc)}
            )
            replay_findings.append(
                Finding(
                    "historical_content_replay",
                    draft["id"],
                    str(exc),
                    gate_code="historical_input_error",
                    dossier_revision=revision or None,
                    summary_id=summary or None,
                )
            )
        replay_rows.append(base)
    criterion = build_criterion(
        bool(replay_rows) and not replay_findings,
        len(replay_rows) - len({f.record_id for f in replay_findings}),
        len(replay_rows),
        tuple(f"draft:{d['draft_id']}" for d in replay_rows),
        replay_findings,
    )
    return replay_rows, criterion
