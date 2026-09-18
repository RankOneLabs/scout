"""Pure projections for the typesafe shadow operator report."""

from __future__ import annotations

import json
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from scout.storage.shadow_relevance import ShadowReportRow
from scout.typesafe.compose import DECIDE_REGISTRY
from scout.typesafe.models import Answers, DecisionRecord


class ReportEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)
    evaluation_id: int
    scan_id: int | None
    shadow_status: str
    shadow_eligible: bool | None
    shadow_p_eligible: float | None
    shadow_uncertain: bool | None
    shadow_reason: str | None
    llm_relevant: bool
    llm_score: float
    human_grade: str | None


class AgreementSummary(BaseModel):
    model_config = ConfigDict(frozen=True)
    total: int
    shadow_llm_agree: int
    shadow_llm_disagree: int
    shadow_human_agree: int
    shadow_human_disagree: int
    human_unavailable: int


class FixtureReplayCheck(BaseModel):
    model_config = ConfigDict(frozen=True)
    passed: bool
    checked: int
    failures: list[str]


class TypesafeReport(BaseModel):
    """Stable JSON model emitted by ``scout typesafe report --json``."""

    model_config = ConfigDict(frozen=True)
    evaluations: list[ReportEvaluation]
    summary: AgreementSummary
    placeholder_fixture_replay: FixtureReplayCheck


class FixtureReplayUnavailableError(RuntimeError):
    """Raised when the packaged acceptance fixture cannot be loaded."""


def replay_acceptance_fixture(path: Path | Traversable | None = None) -> FixtureReplayCheck:
    fixture_path = path or files("scout.typesafe").joinpath("acceptance-cases.json")
    try:
        fixture = json.loads(fixture_path.read_text())
    except (FileNotFoundError, OSError) as exc:
        raise FixtureReplayUnavailableError(
            "packaged typesafe acceptance fixture is unavailable"
        ) from exc
    failures: list[str] = []
    for case in fixture["cases"]:
        name = case["decide"]
        decide = DECIDE_REGISTRY.get(name)
        if decide is None:
            failures.append(f"{name}: decide function is not registered")
            continue
        actual = decide(Answers.model_validate(case["answers"]))
        expected = DecisionRecord.model_validate(case["expected"])
        if actual != expected:
            failures.append(f"{name}: expected {expected.model_dump()}, got {actual.model_dump()}")
    return FixtureReplayCheck(
        passed=not failures, checked=len(fixture["cases"]), failures=failures
    )


def _human_relevant(grade: str | None, llm_relevant: bool) -> bool | None:
    if grade == "correct":
        return llm_relevant
    if grade == "false_positive":
        return False
    if grade == "false_negative":
        return True
    return None


def build_report(rows: list[ShadowReportRow], fixture_check: FixtureReplayCheck) -> TypesafeReport:
    evaluations = [
        ReportEvaluation(
            evaluation_id=row.evaluation_id, scan_id=row.scan_id,
            shadow_status=row.status, shadow_eligible=row.eligible,
            shadow_p_eligible=row.p_eligible, shadow_uncertain=row.uncertain,
            shadow_reason=row.reason if row.status == "ok" else row.error_detail,
            llm_relevant=row.llm_relevant, llm_score=row.llm_score,
            human_grade=row.human_grade,
        )
        for row in rows
    ]
    comparable = [item for item in evaluations if item.shadow_eligible is not None]
    human_pairs = [
        (item, _human_relevant(item.human_grade, item.llm_relevant))
        for item in comparable
    ]
    with_human = [(item, human) for item, human in human_pairs if human is not None]
    return TypesafeReport(
        evaluations=evaluations,
        summary=AgreementSummary(
            total=len(evaluations),
            shadow_llm_agree=sum(i.shadow_eligible == i.llm_relevant for i in comparable),
            shadow_llm_disagree=sum(i.shadow_eligible != i.llm_relevant for i in comparable),
            shadow_human_agree=sum(i.shadow_eligible == human for i, human in with_human),
            shadow_human_disagree=sum(i.shadow_eligible != human for i, human in with_human),
            human_unavailable=len(evaluations) - len(with_human),
        ),
        placeholder_fixture_replay=fixture_check,
    )


def render_text(report: TypesafeReport) -> str:
    lines = []
    for item in report.evaluations:
        lines.append(
            f"evaluation {item.evaluation_id}: shadow={item.shadow_status}/"
            f"eligible={item.shadow_eligible} p={item.shadow_p_eligible} "
            f"uncertain={item.shadow_uncertain} reason={item.shadow_reason!r}; "
            f"llm=relevant={item.llm_relevant} score={item.llm_score}; "
            f"human_grade={item.human_grade}"
        )
    summary = report.summary
    lines.extend([
        "Summary:",
        f"  total={summary.total} shadow_llm_agree={summary.shadow_llm_agree} "
        f"shadow_llm_disagree={summary.shadow_llm_disagree}",
        f"  shadow_human_agree={summary.shadow_human_agree} "
        f"shadow_human_disagree={summary.shadow_human_disagree} "
        f"human_unavailable={summary.human_unavailable}",
        "  placeholder_fixture_replay="
        f"{'PASS' if report.placeholder_fixture_replay.passed else 'FAIL'} "
        f"({report.placeholder_fixture_replay.checked} cases)",
    ])
    lines.extend(f"    {failure}" for failure in report.placeholder_fixture_replay.failures)
    return "\n".join(lines)
