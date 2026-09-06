"""Explicit v1 wire layouts for the assistance producer; never infer model fields."""

from __future__ import annotations

from scout.grading.assistance_types import (
    AssistanceConfig,
    AssistanceOutputs,
    RejectedPopulation,
    ReviewQueue,
)
from scout.grading.snapshots import EVALUATION_WIRE_V1, INPUT_WIRE_V1, POST_WIRE_V1
from scout.grading.wire import ArrayWire, encode_wire_v1, record_wire

CONTEXT_WIRE = next(field.layout for field in INPUT_WIRE_V1.fields if field.name == "context")
REJECTED_WIRE = record_wire(
    "evaluation post context has_grade",
    evaluation=EVALUATION_WIRE_V1,
    post=POST_WIRE_V1,
    context=CONTEXT_WIRE,
)
POPULATION_WIRE = record_wire("format project_key items", items=ArrayWire(REJECTED_WIRE))
CONFIG_WIRE = record_wire(
    "format seed heldout_fraction ranked random",
    ranked=record_wire("kind count regularization_c class_weight max_features max_iterations"),
    random=record_wire("kind count seed design"),
)
PROVENANCE_WIRE = record_wire("queue_digest method")
PARTITION_WIRE = record_wire(
    "format snapshot_digest members",
    members=ArrayWire(
        record_wire(
            "evaluation_id input_digest group_id partition exposed provenance",
            provenance=ArrayWire(PROVENANCE_WIRE),
        )
    ),
)
MODEL_WIRE = record_wire(
    "format train_evaluation_ids vocabulary idf coefficients intercept iterations"
)
SCORE_WIRE = record_wire(
    "evaluation_id probability explanation",
    explanation=ArrayWire(record_wire("term contribution")),
)
SELECTOR_WIRE = record_wire(
    "kind population_evaluation_ids selected_evaluation_ids scores explanation_method",
    scores=ArrayWire(SCORE_WIRE),
)
QUEUE_WIRE = record_wire(
    "format project_key population_digest items ranked random",
    items=ArrayWire(
        record_wire(
            "duplicate_key sources",
            sources=ArrayWire(record_wire("evaluation_id ranked_position random_position")),
        )
    ),
    ranked=SELECTOR_WIRE,
    random=SELECTOR_WIRE,
)
CONFUSION_WIRE = record_wire("true_positive true_negative false_positive false_negative")
REPORT_WIRE = record_wire(
    "format project_key source_count candidate_count candidate_duplicate_count "
    "selected_evaluation_count queue_item_count "
    "duplicates_removed overlap_count exclusions heldout",
    exclusions=ArrayWire(record_wire("evaluation_id reason")),
    heldout=record_wire(
        "sample_count random_provenance_count classifier train_majority_baseline",
        classifier=CONFUSION_WIRE,
        train_majority_baseline=CONFUSION_WIRE,
    ),
)
OBSERVATION_WIRE = record_wire(
    "format queue_digest lineage_digest observed_at elapsed_ms cpu_ms process_peak_rss_bytes"
)


def encode_population(population: RejectedPopulation) -> bytes:
    return encode_wire_v1(population, POPULATION_WIRE)


def encode_config(config: AssistanceConfig) -> bytes:
    return encode_wire_v1(config, CONFIG_WIRE)


def encode_queue(queue: ReviewQueue) -> bytes:
    return encode_wire_v1(queue, QUEUE_WIRE)


def encode_outputs(outputs: AssistanceOutputs) -> tuple[bytes, ...]:
    """Four ordered outputs; random-only runs retain an explicit null model."""
    return (
        encode_wire_v1(outputs.partition, PARTITION_WIRE),
        encode_wire_v1(outputs.model, MODEL_WIRE),
        encode_queue(outputs.queue),
        encode_wire_v1(outputs.report, REPORT_WIRE),
    )
