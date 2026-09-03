"""Parity gate: the Jig grade-projection dimension vector must never drift
from the one grading contract (web/grading_schema.json).

config.FAILURE_DIMENSIONS is already sourced from
web/grading_schema.json's $defs/failureDimension enum at import time (see
config.py). scout.evals.phase1.export_adapter.CANONICAL_FAILURE_DIMENSIONS derives
from scout.config.FAILURE_DIMENSIONS rather than duplicating the literal — this
test locks that chain end to end so a future edit that reintroduces a
duplicate literal, drops a dimension, or desyncs the schema's enum order
fails loudly here instead of silently producing a mis-shaped Jig database.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scout.config import FAILURE_DIMENSIONS, HUMAN_GRADE_SCHEMA_VERSION, GradeRecord
from scout.evals.phase1.export_adapter import CANONICAL_FAILURE_DIMENSIONS

_SCHEMA_PATH = Path(__file__).parent.parent / "web" / "grading_schema.json"


def _schema_failure_dimensions() -> tuple[str, ...]:
    schema = json.loads(_SCHEMA_PATH.read_text())
    return tuple(schema["$defs"]["failureDimension"]["enum"])


def test_schema_defines_exactly_seven_unique_failure_dimensions() -> None:
    dims = _schema_failure_dimensions()
    assert len(dims) == 7
    assert len(set(dims)) == 7


def test_config_failure_dimensions_match_schema_order() -> None:
    assert _schema_failure_dimensions() == FAILURE_DIMENSIONS


def test_canonical_failure_dimensions_match_config_order() -> None:
    assert CANONICAL_FAILURE_DIMENSIONS == FAILURE_DIMENSIONS


def test_canonical_failure_dimensions_has_exactly_seven_unique_values() -> None:
    assert len(CANONICAL_FAILURE_DIMENSIONS) == 7
    assert len(set(CANONICAL_FAILURE_DIMENSIONS)) == 7


def test_human_grade_schema_version_is_the_paa_response_quality_producer_version() -> None:
    """HUMAN_GRADE_SCHEMA_VERSION is the single code constant grading.py,
    state_manager.py's grade-write checks, and paa_declarations.py's
    PRODUCER_REGISTRY all resolve human grading against — locked here so a
    future edit can't silently fork it back into a scattered literal."""
    assert HUMAN_GRADE_SCHEMA_VERSION == 3
    assert GradeRecord.__dataclass_fields__["schema_version"].default == HUMAN_GRADE_SCHEMA_VERSION


def test_guard_rejects_a_longer_sequence_with_only_seven_unique_values() -> None:
    """An 8-element FAILURE_DIMENSIONS with one duplicate has 7 *unique*
    values but is not "exactly seven values" — the module-level guard must
    reject the length mismatch, not just the uniqueness count."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import scout.config; "
            "scout.config.FAILURE_DIMENSIONS = ("
            "    'contextual_understanding', 'factual_support', "
            "    'unsupported_implication', 'posture', 'tone', 'wording', 'usefulness', "
            "    'tone'"
            "); "
            "import scout.evals.phase1.export_adapter",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "exactly seven unique values" in result.stderr
