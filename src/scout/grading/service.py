"""Manual grading for scout evaluations — v3 causal grading."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from scout.config import (
    FAILURE_DIMENSIONS,
    GRADING_SCHEMA,
    HUMAN_GRADE_SCHEMA_VERSION,
    POSTURES,
    GradeRecord,
    GradingSignal,
)
from scout.grading.editor import resolve_editor
from scout.storage.state import StateManager

# Compiled once at module load. GRADING_SCHEMA is checked as a valid Draft
# 2020-12 document by Draft202012Validator.check_schema below; a malformed
# artifact fails fast at import time rather than at first grade write.
Draft202012Validator.check_schema(GRADING_SCHEMA)
_ENVELOPE_VALIDATOR = Draft202012Validator(GRADING_SCHEMA)


def _envelope_errors(instance: dict[str, Any]) -> list[str]:
    """Validate a {grade, evaluation_posture} envelope against the shared
    schema. Returns error strings sorted deterministically by (instance
    path, schema path) so callers get stable, reproducible output."""
    errors = sorted(
        _ENVELOPE_VALIDATOR.iter_errors(instance),
        key=lambda e: (list(e.absolute_path), list(e.absolute_schema_path)),
    )
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in errors
    ]


def validate_grade_envelope(
    grade: Mapping[str, Any], evaluation_posture: str | None
) -> list[str]:
    """Validate a raw, unfiltered grade payload (e.g. a sidecar request
    body) plus the evaluation's stored posture against the shared schema.

    Takes the payload mapping as-is rather than named keyword arguments,
    so unknown/legacy fields the caller didn't strip are still rejected by
    additionalProperties: false rather than silently dropped before
    reaching the validator. This is the sole ordinary grade contract
    entry point — see grade_envelope_payload for the GradeRecord adapter
    every caller routes through.
    """
    return _envelope_errors({"grade": dict(grade), "evaluation_posture": evaluation_posture})


def grade_envelope_payload(grade: GradeRecord) -> dict[str, Any]:
    """Project a GradeRecord's contract-relevant fields into the raw
    {relevance_judgment, ...} payload validate_grade_envelope expects.

    The sole GradeRecord -> payload adapter: CLI construction
    (review_scan) and StateManager's persistence-time revalidation both
    route through this, so neither can silently validate a stripped-down
    copy that drops a field (e.g. edited_text) before it ever reaches the
    schema.
    """
    return {
        "relevance_judgment": grade.relevance_judgment,
        "action_judgment": grade.action_judgment,
        "dimensions": grade.dimensions,
        "failure_note": grade.failure_note,
        "factual_offending_claim": grade.factual_offending_claim,
        "factual_disposition": grade.factual_disposition,
        "factual_contradicting_evidence": grade.factual_contradicting_evidence,
        "context_missing_input": grade.context_missing_input,
        "posture_should_have_been": grade.posture_should_have_been,
        "implication_implied_claim": grade.implication_implied_claim,
        "implication_missing_support": grade.implication_missing_support,
        "edited_text": grade.edited_text,
    }


# --- Signal Formatting ---


def format_grading_signals(signal: GradingSignal) -> str:
    """Format aggregated v2 grading signal for prompt injection."""
    if signal.total_graded == 0:
        return ""

    parts = [
        f"Recent reviews: {signal.pass_count} of {signal.total_graded} passed."
    ]

    if signal.false_positive_count > 0:
        parts.append(f"{signal.false_positive_count} false positives.")
    if signal.false_negative_count > 0:
        parts.append(f"{signal.false_negative_count} false negatives.")

    if signal.dimension_counts:
        dim_strs = [f"{d} ({c})" for d, c in signal.dimension_counts[:3]]
        parts.append("Top failure dimensions: " + ", ".join(dim_strs) + ".")

    if signal.posture_correction_count > 0:
        parts.append(f"{signal.posture_correction_count} posture corrections.")

    if signal.factual_unsupported_count > 0 or signal.factual_contradicted_count > 0:
        parts.append(
            f"Factual issues: {signal.factual_unsupported_count} unsupported, "
            f"{signal.factual_contradicted_count} contradicted."
        )

    if signal.recent_causal_examples:
        examples = "; ".join(signal.recent_causal_examples[:2])
        parts.append(f"Recent failure notes: {examples}.")

    return " ".join(parts)


# --- CLI Review ---

_DIMENSION_MENU = "\n".join(
    f"  ({i + 1}) {dim}" for i, dim in enumerate(FAILURE_DIMENSIONS)
)
_POSTURE_MENU = "\n".join(
    f"  ({i + 1}) {p}" for i, p in enumerate(POSTURES)
)


def format_review_item(item: Mapping[str, Any], index: int, total: int) -> str:
    """Format a gradeable evaluation item for terminal display."""
    score = item.get("score")
    score_str = f"{score:.2f}" if score is not None else "n/a"
    content = item.get("content") or ""
    if len(content) > 500:
        content = content[:497] + "..."

    lines = [
        f"\n{'━' * 3} Evaluation {index + 1}/{total} {'━' * 36}",
        f"#{item.get('channel_name', '?')} · "
        f"{item.get('author_name', '?')} · "
        f"{item.get('created_at', '?')}",
        "",
        f"  {content}",
        "",
        f"Relevance score: {score_str} | "
        f"Relevant: {item.get('relevant', '?')} | "
        f"Surface: {item.get('surface_status', 'n/a')} | "
        f"Posture: {item.get('posture', 'n/a')}",
    ]

    project = item.get("project_key")
    dossier_rev = item.get("dossier_revision")
    if project or dossier_rev:
        lines.append(
            f"Project: {project or '?'} | Dossier rev: {(dossier_rev or 'n/a')[:12]}"
        )

    draft = item.get("comment_text")
    if draft:
        lines.append(f"Draft: {draft[:200]}")
        verdict = item.get("verdict")
        if verdict:
            lines.append(f"Critic: {verdict}")

    existing = item.get("relevance_judgment")
    existing_action = item.get("action_judgment")
    if existing:
        lines.append(
            f"\n  [Already graded: {existing} / {existing_action or 'v1'}]"
        )

    return "\n".join(lines)


def _prompt_required(prompt: str) -> str:
    """Prompt until non-whitespace input is entered. The CLI never manufactures
    a placeholder value for a required field — it re-asks until one is given."""
    while True:
        value = input(prompt).strip()
        if value:
            return value
        print("  This field is required — please enter a non-empty value.")


def _prompt_relevance() -> str:
    """Prompt for relevance_judgment. Returns 'correct', 'false_positive',
    'false_negative', '__skip__', or '__quit__'."""
    while True:
        raw = input(
            "\nRelevance [c=correct/p=false_positive/n=false_negative/s=skip/q=quit]: "
        ).strip().lower()
        if raw in ("q", "quit"):
            return "__quit__"
        if raw in ("s", "skip"):
            return "__skip__"
        if raw in ("c", "correct"):
            return "correct"
        if raw in ("p", "false_positive"):
            return "false_positive"
        if raw in ("n", "false_negative"):
            return "false_negative"
        print("  Invalid choice — enter c, p, n, s, or q.")


def _prompt_pass_or_fail(relevance: str) -> str:
    """Prompt for action_judgment. Returns 'accept' or 'fail'."""
    if relevance in ("false_positive", "false_negative"):
        print("  (incorrect relevance — action must be fail)")
        return "fail"
    while True:
        answer = input("\nPass (accept) or fail? [a/f]: ").strip().lower()
        if answer in ("a", "accept"):
            return "accept"
        if answer in ("f", "fail"):
            return "fail"
        print("  Invalid choice — enter a or f.")


def _prompt_dimensions() -> list[str]:
    """Prompt for failure dimensions (multi-select). Loops until at least
    one valid, unique dimension is selected."""
    while True:
        print(f"\nSelect failure dimensions (comma-separated numbers):\n{_DIMENSION_MENU}")
        raw = input("> ").strip()
        selected: list[str] = []
        seen: set[str] = set()
        for part in raw.split(","):
            part = part.strip()
            try:
                idx = int(part) - 1
                if 0 <= idx < len(FAILURE_DIMENSIONS):
                    dim = FAILURE_DIMENSIONS[idx]
                    if dim not in seen:
                        selected.append(dim)
                        seen.add(dim)
            except ValueError:
                pass
        if selected:
            return selected
        print("  At least one valid dimension is required.")


def _prompt_factual_detail() -> tuple[str, str, str | None]:
    """Returns (offending_claim, disposition, contradicting_evidence|None).
    Contradicted requires evidence; unsupported forbids it (never asked)."""
    claim = _prompt_required("  Offending claim: ")
    while True:
        disp_raw = input("  Disposition — (u)nsupported / (c)ontradicted: ").strip().lower()
        if disp_raw in ("u", "unsupported"):
            return claim, "unsupported", None
        if disp_raw in ("c", "contradicted"):
            evidence = _prompt_required("  Contradicting evidence: ")
            return claim, "contradicted", evidence
        print("  Invalid choice — enter u or c.")


def _prompt_posture(evaluation_posture: str | None) -> str:
    """Returns the corrected posture. Loops until a valid posture is chosen
    that differs from the evaluation's actual posture."""
    if evaluation_posture:
        print(f"  (evaluation posture was: {evaluation_posture})")
    while True:
        print(f"  Should have been:\n{_POSTURE_MENU}")
        raw = input("  > ").strip()
        try:
            idx = int(raw) - 1
        except ValueError:
            idx = -1
        if not (0 <= idx < len(POSTURES)):
            print("  Invalid choice — enter a listed posture number.")
            continue
        posture = POSTURES[idx]
        if evaluation_posture and posture == evaluation_posture:
            print("  Must differ from the evaluation posture — choose another.")
            continue
        return posture


def _prompt_edited_text(draft_text: str | None) -> str | None:
    """Open a single-buffer $EDITOR session, seeded with the machine draft
    (or empty, when the evaluation has none), to optionally capture a
    corrected reply for a fail. Opened at most once per graded item.

    Unchanged or blank content after normalization means no edit — returns
    None rather than a no-op or empty string, so a grader who doesn't want
    to correct the reply text can simply save-and-exit without touching
    anything. A non-blank, changed buffer is returned stripped of leading/
    trailing whitespace only; internal formatting is preserved as typed.
    """
    seed = draft_text or ""
    editor_argv = resolve_editor()
    fd, tmpname = tempfile.mkstemp(suffix=".md", prefix="scout-grade-")
    tmppath = Path(tmpname)
    try:
        os.close(fd)
        tmppath.write_text(seed)
        subprocess.run([*editor_argv, str(tmppath)], check=True)
        new_text = tmppath.read_text()
    finally:
        tmppath.unlink(missing_ok=True)

    normalized = new_text.strip()
    if not normalized or normalized == seed.strip():
        return None
    return normalized


def review_scan(state: StateManager, scan_id: int) -> None:
    """Interactive CLI review of a scan's evaluations (v3 causal grading)."""
    items = state.get_gradeable_items(scan_id)
    if not items:
        print(f"No evaluations found for scan #{scan_id}")
        return

    graded_count, total = state.get_grading_progress(scan_id)
    print(
        f"\nReviewing scan #{scan_id}: {len(items)} evaluations "
        f"({graded_count}/{total} graded)"
    )

    session_graded = 0
    for i, item in enumerate(items):
        print(format_review_item(item, i, len(items)))

        relevance = _prompt_relevance()

        if relevance == "__quit__":
            print(f"\nQuitting. Graded {session_graded} items this session.")
            return
        if relevance == "__skip__":
            continue

        action = _prompt_pass_or_fail(relevance)
        evaluation_posture = str(item.get("posture")) if item.get("posture") else None

        post_id = item["post_id"]
        evaluation_id = item["evaluation_id"]
        if not isinstance(post_id, int):
            raise ValueError("gradeable item has an invalid post_id")
        if evaluation_id is not None and not isinstance(evaluation_id, int):
            raise ValueError("gradeable item has an invalid evaluation_id")

        # Captured once, outside the retry loop below, so a validation
        # failure on an unrelated field (missing dimension detail, etc.)
        # never re-opens the editor or discards what was already typed.
        # Without a draft there is nothing to correct — StateManager
        # rejects edited_text when the evaluation has no draft_comments
        # row, so the editor is never even offered in that case rather
        # than letting the grader type something save_grade will bounce.
        draft_text = item.get("comment_text")
        edited_text = (
            _prompt_edited_text(str(draft_text) if draft_text is not None else None)
            if action == "fail" and item.get("draft_id") is not None
            else None
        )

        # Gather causal detail for a fail, then re-validate the fully
        # assembled record before persisting. On failure, re-enter the
        # detail prompts rather than persisting partial input — the
        # authoritative boundary check lives in StateManager.save_grade,
        # but catching combinations here saves the operator a re-run.
        while True:
            dimensions: list[str] | None = None
            failure_note: str | None = None
            factual_offending_claim: str | None = None
            factual_disposition: str | None = None
            factual_contradicting_evidence: str | None = None
            context_missing_input: str | None = None
            posture_should_have_been: str | None = None
            implication_implied_claim: str | None = None
            implication_missing_support: str | None = None

            if action == "fail":
                dimensions = _prompt_dimensions()
                failure_note = _prompt_required("\nFailure note (required): ")

                if "factual_support" in dimensions:
                    print("\n  Factual support detail:")
                    (
                        factual_offending_claim,
                        factual_disposition,
                        factual_contradicting_evidence,
                    ) = _prompt_factual_detail()
                if "contextual_understanding" in dimensions:
                    context_missing_input = _prompt_required(
                        "\n  Context: what was missing? "
                    )
                if "posture" in dimensions:
                    posture_should_have_been = _prompt_posture(evaluation_posture)
                if "unsupported_implication" in dimensions:
                    implication_implied_claim = _prompt_required("\n  Implied claim: ")
                    implication_missing_support = _prompt_required("  Missing support: ")

            grade = GradeRecord(
                post_id=post_id,
                evaluation_id=evaluation_id,
                source="cli",
                graded_at=datetime.now(UTC),
                relevance_judgment=relevance,
                action_judgment=action,
                scan_id=scan_id,
                schema_version=HUMAN_GRADE_SCHEMA_VERSION,
                dimensions=dimensions,
                failure_note=failure_note,
                factual_offending_claim=factual_offending_claim,
                factual_disposition=factual_disposition,
                factual_contradicting_evidence=factual_contradicting_evidence,
                context_missing_input=context_missing_input,
                posture_should_have_been=posture_should_have_been,
                implication_implied_claim=implication_implied_claim,
                implication_missing_support=implication_missing_support,
                edited_text=edited_text,
            )
            errors = validate_grade_envelope(
                grade_envelope_payload(grade), evaluation_posture
            )
            if errors:
                print("\n  Invalid grade — please correct:")
                for err in errors:
                    print(f"    - {err}")
                continue
            break

        state.save_grade(grade)
        state.commit()
        session_graded += 1
        graded, total = state.get_grading_progress(scan_id)
        print(f"  Graded {graded}/{total} evaluations")

    print(f"\nDone. Graded {session_graded} evaluations this session.")
