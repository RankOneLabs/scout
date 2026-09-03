"""Scout-native reply_draft correction grading.

A versioned, deterministic Unicode-code-point edit-distance metric between
an assembled draft and a human grader's corrected reply text
(``normalized_edit_distance/v1``), and ``ReplyCorrectionGrader`` — the
``jig.Grader`` replay/experiments.py attaches to a candidate
reply_draft replay so it scores the candidate's live output against that
pinned correction. The historical baseline's distance is computed
independently (see replay/experiments.py), from its own already-stored
root output — the baseline replay never had this grader attached, so
nothing here re-executes or re-scores it.
"""

from __future__ import annotations

from typing import Any

from jig import Grader, Score, ScoreSource

from scout.dossiers.resolver import DossierSummary
from scout.scanning.schemas import StructuredDraftOutput
from scout.verifier import DRAFT_TEXT_ASSEMBLER_VERSION, assemble_draft_text

NORMALIZED_EDIT_DISTANCE_GRADER_VERSION = "normalized_edit_distance/v1"


def _levenshtein(a: str, b: str) -> int:
    """Unicode-code-point Levenshtein edit distance between `a` and `b`.

    Python string indexing is already code-point based, so no manual
    surrogate-pair handling is needed. Single-row DP, O(len(a) * len(b)).
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous_row = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current_row = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            insert_cost = current_row[j - 1] + 1
            delete_cost = previous_row[j] + 1
            substitute_cost = previous_row[j - 1] + (ca != cb)
            current_row[j] = min(insert_cost, delete_cost, substitute_cost)
        previous_row = current_row
    return previous_row[len(b)]


def normalized_edit_distance(output_text: str | None, correction_text: str) -> float:
    """normalized_edit_distance/v1.

    Unicode-code-point Levenshtein distance between `output_text` (treated
    as empty when None — an abstain or otherwise empty-assembling draft)
    and `correction_text`, divided by
    max(len(output_text), len(correction_text), 1). The versioned formula
    is deterministic for Unicode text of any length, and a negative
    candidate_distance - baseline_distance delta unambiguously means the
    candidate is closer to the correction than the baseline was.
    """
    output = output_text or ""
    distance = _levenshtein(output, correction_text)
    denominator = max(len(output), len(correction_text), 1)
    return distance / denominator


class ReplyCorrectionGrader(Grader[StructuredDraftOutput]):  # type: ignore[misc]
    """Live jig.Grader scoring a candidate reply_draft replay's
    StructuredDraftOutput against one pinned human correction.

    Bound at construction to the exact dossier and correction text the
    replay was authorized against. jig_replay runs this grader for real
    against the candidate's live output — never replayed from a recording
    — immediately after a successful, schema-valid agent run; a malformed
    or errored candidate result never reaches grade() at all.
    """

    def __init__(self, *, dossier: DossierSummary, correction_text: str) -> None:
        self._dossier = dossier
        self._correction_text = correction_text

    async def grade(
        self,
        input: Any,
        output: StructuredDraftOutput,
        context: dict[str, Any] | None = None,
    ) -> list[Score]:
        assembled = assemble_draft_text(output, self._dossier)
        distance = normalized_edit_distance(assembled, self._correction_text)
        return [
            Score(
                dimension=NORMALIZED_EDIT_DISTANCE_GRADER_VERSION,
                value=distance,
                source=ScoreSource.GROUND_TRUTH,
                metadata={"assembler_version": DRAFT_TEXT_ASSEMBLER_VERSION},
            )
        ]


__all__ = [
    "NORMALIZED_EDIT_DISTANCE_GRADER_VERSION",
    "ReplyCorrectionGrader",
    "normalized_edit_distance",
]
