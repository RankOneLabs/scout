"""Scout's producer registry: declared evaluator identity -> Scout code.

This is the one piece of PAA knowledge that is genuinely Scout's and not
the contract's. The declaration says a verdict of some identity exists;
this says which Scout code produces it, and at which version. paa_runtime
deliberately ships no registry of its own and takes one as a parameter —
a registry is a property of the consumer, not of the architecture.

Every evaluator any checked-in declaration references must appear here
exactly once, matched on the full identity tuple (property, target,
technique, evaluation_basis, epistemic_status, version, authority). A
declaration naming an identity absent from this registry fails to load;
there is no silent fallback to an unregistered producer.

``status`` is "implemented" when ``version`` is sourced from a live
producer-version constant beside the code that produces the verdict, or
"future" for an evaluator a checked-in declaration already references but
no cohort has built yet (see docs/architecture.md).

On the two-axis identity: ``evaluation_basis`` names how a verdict is
grounded (which invariant set, which rubric, which human-gold protocol)
and ``epistemic_status`` names whether governance treats it as the task's
authoritative truth signal or an approximation of one. These used to be a
single ``oracle`` field here, which could not tell a rubric-graded proxy
from a rubric-graded ground truth — they collapsed to one identity, and
two genuinely different producers became indistinguishable.
"""

from __future__ import annotations

from paa_runtime.declarations import PaaEvaluationBasis, ProducerRegistration

from scout.config import HUMAN_GRADE_SCHEMA_VERSION
from scout.grading.correction import NORMALIZED_EDIT_DISTANCE_GRADER_VERSION
from scout.scanning.agent import LLM_CRITIC_PROMPT_VERSION
from scout.storage.state import AUTHOR_RATE_EVALUATOR_VERSION
from scout.verifier import CONTENT_INVARIANTS_EVALUATOR_VERSION

__all__ = ["PRODUCER_REGISTRY"]


PRODUCER_REGISTRY: tuple[ProducerRegistration, ...] = (
    ProducerRegistration(
        property="correction_distance", target="output", technique="deterministic",
        evaluation_basis=PaaEvaluationBasis(kind="reference_label", ref="pinned_reply_correction"),
        epistemic_status="proxy", version=NORMALIZED_EDIT_DISTANCE_GRADER_VERSION,
        authority="advisory", status="implemented",
    ),
    ProducerRegistration(
        property="content_invariants", target="output", technique="deterministic",
        evaluation_basis=PaaEvaluationBasis(kind="invariant", ref="content_invariants"),
        epistemic_status="ground_truth", version=CONTENT_INVARIANTS_EVALUATOR_VERSION,
        authority="blocking", status="implemented",
    ),
    ProducerRegistration(
        property="author_rate", target="process", technique="deterministic",
        evaluation_basis=PaaEvaluationBasis(kind="invariant", ref="author_rate"),
        epistemic_status="ground_truth", version=AUTHOR_RATE_EVALUATOR_VERSION,
        authority="blocking", status="implemented",
    ),
    ProducerRegistration(
        property="response_quality", target="output", technique="llm_judge",
        evaluation_basis=PaaEvaluationBasis(kind="rubric", ref="response_quality_rubric"),
        epistemic_status="proxy", version=LLM_CRITIC_PROMPT_VERSION,
        authority="advisory", status="implemented",
    ),
    ProducerRegistration(
        property="response_quality", target="output", technique="human",
        evaluation_basis=PaaEvaluationBasis(kind="human_gold", ref="response_quality_human_gold"),
        epistemic_status="ground_truth", version=str(HUMAN_GRADE_SCHEMA_VERSION),
        authority="advisory", status="implemented",
    ),
    ProducerRegistration(
        property="claim_admissibility", target="input", technique="deterministic",
        evaluation_basis=PaaEvaluationBasis(kind="invariant", ref="claim_admissibility"),
        epistemic_status="ground_truth", version="1",
        authority="blocking", status="future",
    ),
    ProducerRegistration(
        property="canonical_truth", target="output", technique="human",
        evaluation_basis=PaaEvaluationBasis(kind="human_gold", ref="canonical_truth_human_gold"),
        epistemic_status="ground_truth", version="1",
        authority="blocking", status="future",
    ),
)
