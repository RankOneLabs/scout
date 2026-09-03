"""Contract tests for Jig's complete-output TraceDiff comparison.

Brief b33507f1-ef1e-4f87-adc3-73825c4d67e8 pins Scout to a reviewed Jig
revision whose ``TraceDiff`` compares *complete* canonical ``submit_output``
payloads rather than the 200-character root-span output preview. Scout's
pin (``pyproject.toml`` / ``uv.lock``) now points at
``4fae89bb04768d57be6db4cd2bdef859d1e17322``
(github.com/RankOneLabs/jig/pull/76, reviewed and merged upstream), which
lands this on ``jig.replay.diff.TraceDiff``:

- ``comparison_complete: bool`` and ``comparison_incomplete_reason: str |
  None`` — ``None`` only when both sides canonicalized a validated
  structured value; otherwise ``"preview_only_output"`` (the root predates
  complete-output capture) or ``"structured_output_unavailable"`` (a
  current-format trace with no structured source, e.g. a plain-text run).
- Per side: ``a_output_hash`` / ``b_output_hash`` (canonical SHA-256),
  ``a_output_byte_length`` / ``b_output_byte_length`` (canonical UTF-8
  byte length), ``a_output_preview`` / ``b_output_preview`` (the existing
  200-char preview), and ``a_output_complete`` / ``b_output_complete``
  (the actual JSON-native complete value, for downstream Scout domain-diff
  construction).
- ``identical`` now requires ``comparison_complete`` and matching
  ``*_output_hash`` / ``*_output_byte_length`` for its output dimension —
  the 200-char preview (``output_diff``, unchanged in behavior) is never
  consulted for equality.

This module is the hermetic gate for that contract: the reference
canonicalization tests below (``canonical_json_bytes`` /
``canonical_output_hash``) prove the semantics Scout requires are
well-defined independently of Jig, and ``TestCompleteOutputComparisonContract``
exercises the real pinned ``jig.replay.diff.trace_diff`` against synthetic
root spans shaped like what the reviewed runner persists (see
``ROOT_OUTPUT_KIND_KEY`` / ``ROOT_OUTPUT_COMPLETE_KEY`` /
``ROOT_OUTPUT_SHA256_KEY`` / ``ROOT_OUTPUT_BYTE_LENGTH_KEY`` in
``jig.core.runner``).
"""
from __future__ import annotations

import hashlib
import json
import math
import tomllib
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jig import SpanKind, TraceDiff, replay, trace_diff
from jig.core.types import Span, TracingLogger

from scout.replay.experiments import JIG_REVISION

# --- Reference canonicalization (the equality Scout requires Jig to
# implement — see the brief's canonicalization decision) ---


def _reject_non_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite float in submit_output payload: {value!r}")
    if isinstance(value, dict):
        for v in value.values():
            _reject_non_finite(v)
    elif isinstance(value, list):
        for v in value:
            _reject_non_finite(v)


def canonical_json_bytes(value: object) -> bytes:
    """UTF-8, sorted keys, compact separators, Unicode preserved, non-finite rejected."""
    _reject_non_finite(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def canonical_output_hash(value: object) -> tuple[str, int]:
    """Return (sha256_hexdigest, utf8_byte_length) of the canonical encoding."""
    payload = canonical_json_bytes(value)
    return hashlib.sha256(payload).hexdigest(), len(payload)


# --- Tests of the reference canonicalization itself: these run
# unconditionally and prove the contract Scout wants is well-defined,
# independent of whether Jig has adopted it yet. ---


def test_canonical_hash_is_key_order_independent() -> None:
    a = {"b": 1, "a": {"y": 2, "x": 1}}
    b = {"a": {"x": 1, "y": 2}, "b": 1}
    assert canonical_output_hash(a) == canonical_output_hash(b)


def test_canonical_hash_preserves_unicode_without_escaping() -> None:
    payload = {"text": "café ☃ snowman"}
    encoded = canonical_json_bytes(payload)
    assert "café".encode() in encoded
    assert b"\\u" not in encoded


def test_canonical_hash_same_200char_preview_different_tail_diverges() -> None:
    preview = "x" * 200
    a = {"output": preview + "-branch-A-tail"}
    b = {"output": preview + "-branch-B-tail"}
    assert a["output"][:200] == b["output"][:200]
    a_hash, _a_len = canonical_output_hash(a)
    b_hash, _b_len = canonical_output_hash(b)
    assert a_hash != b_hash


def test_canonical_hash_matches_for_identical_complete_payloads() -> None:
    a = {"output": "same complete payload", "n": 3}
    b = {"n": 3, "output": "same complete payload"}
    assert canonical_output_hash(a) == canonical_output_hash(b)


def test_canonical_hash_rejects_nan() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json_bytes({"score": float("nan")})


def test_canonical_hash_rejects_infinity() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json_bytes({"score": float("inf")})


def test_canonical_hash_rejects_nested_non_finite() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json_bytes({"outer": [{"inner": float("-inf")}]})


# --- Public export contract: this is the one fixture from the brief's
# "Next action" that already passes today — jig.replay / TraceDiff /
# trace_diff are part of the stable public surface at the current pin. ---


def test_jig_exports_replay_trace_diff_and_trace_diff_publicly() -> None:
    from jig import TraceDiff as _TD  # noqa: F401
    from jig import replay as _replay  # noqa: F401
    from jig import trace_diff as _trace_diff  # noqa: F401
    from jig.replay import TraceDiff as _TD2  # noqa: F401
    from jig.replay import replay as _replay2  # noqa: F401
    from jig.replay import trace_diff as _trace_diff2  # noqa: F401

    assert _TD is _TD2
    assert _replay is _replay2
    assert _trace_diff is _trace_diff2


# --- Upstream feature-detection: field presence on TraceDiff rather than a
# version string, since Jig has no contract version marker. Kept (not
# deleted outright) so a future Jig bump that regresses the contract fails
# loudly here instead of the fixtures below just erroring on missing
# attributes. ---

_REQUIRED_COMPLETE_OUTPUT_FIELDS = frozenset(
    {
        "comparison_complete",
        "comparison_incomplete_reason",
        "a_output_hash",
        "b_output_hash",
        "a_output_byte_length",
        "b_output_byte_length",
    }
)


def jig_has_complete_output_contract() -> bool:
    """Feature-detect the complete-output contract on the installed Jig."""
    present = {f.name for f in fields(TraceDiff)}
    return _REQUIRED_COMPLETE_OUTPUT_FIELDS.issubset(present)


def test_pinned_jig_ships_the_complete_output_contract() -> None:
    """Scout's pin (pyproject.toml / uv.lock) must actually carry the
    fields TestCompleteOutputComparisonContract below depends on — this
    fails loudly if a future re-lock silently regresses the pin."""
    assert jig_has_complete_output_contract()


def test_evaluation_experiments_jig_revision_matches_the_locked_pin() -> None:
    """scout.replay.experiments.JIG_REVISION is persisted verbatim onto every
    trace_comparisons row — it must be the exact reviewed commit SHA
    pyproject.toml/uv.lock actually pin, not a stale copy-paste."""
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    jig_dependency = next(
        dependency
        for dependency in project["project"]["dependencies"]
        if dependency.startswith("jig[")
    )
    revision = jig_dependency.rsplit("@", 1)[-1]

    assert revision == JIG_REVISION
    assert JIG_REVISION in (root / "uv.lock").read_text()


class _StubTracer(TracingLogger):
    """Serves a pre-built span list per trace_id — mirrors jig's own
    tests/test_trace_diff.py::_StubTracer so these fixtures stay aligned
    with upstream's hermetic test style."""

    def __init__(self, traces: dict[str, list[Span]]) -> None:
        self._traces = traces

    def start_trace(self, name: str, metadata: Any = None, kind: Any = SpanKind.AGENT_RUN) -> Any:
        raise NotImplementedError

    def start_span(
        self,
        parent_id: str | None,
        kind: Any,
        name: str,
        input: Any = None,  # noqa: A002
        metadata: Any = None,
    ) -> Any:
        raise NotImplementedError

    def end_span(
        self, span_id: str, output: Any = None, error: Any = None, usage: Any = None
    ) -> Any:
        raise NotImplementedError

    async def get_trace(self, trace_id: str) -> list[Span]:
        return self._traces.get(trace_id, [])

    async def list_traces(self, since: Any = None, limit: int = 50, name: Any = None) -> list[Any]:
        return []

    async def flush(self) -> None:
        pass


def _preview_of(complete_payload: dict[str, Any]) -> str:
    """The 200-char preview a complete-output-aware Jig would still display —
    shared by ``_root_with_complete_output`` and its callers so a test can
    assert two payloads share a preview without duplicating (and risking
    drift from) the truncation logic itself."""
    return json.dumps(complete_payload, sort_keys=True)[:200]


def _root_with_complete_output(
    trace_id: str, *, complete_payload: dict[str, Any], preview_only: bool = False
) -> Span:
    """Build a synthetic AGENT_RUN root span shaped like what the reviewed
    Jig runner persists (see ``jig.core.runner.ROOT_OUTPUT_*_KEY``).

    ``preview_only=True`` models a historical trace recorded before Jig
    captured complete output — no ``output_kind`` marker at all, only the
    truncated preview, matching the ``"preview_only_output"`` reason.
    """
    preview = _preview_of(complete_payload)
    out: dict[str, Any] = {"output": preview, "scores": None}
    if not preview_only:
        output_hash, output_len = canonical_output_hash(complete_payload)
        out["output_kind"] = "structured"
        out["output_complete"] = complete_payload
        out["output_sha256"] = output_hash
        out["output_byte_length"] = output_len
    return Span(
        id=f"{trace_id}-root",
        trace_id=trace_id,
        kind=SpanKind.AGENT_RUN,
        name="agent",
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        duration_ms=10.0,
        input=None,
        output=out,
    )


class TestCompleteOutputComparisonContract:
    """Exercises the real pinned ``jig.replay.diff.trace_diff`` against
    synthetic root spans: same-preview/different-tail output, absent full
    output (preview_only_output vs. structured_output_unavailable), and
    tool-empty phases."""

    async def test_identical_false_when_complete_hashes_differ_same_preview(self) -> None:
        preview_prefix = {"kind": "note", "body": "x" * 190}
        a_payload = {**preview_prefix, "tail": "AAAA"}
        b_payload = {**preview_prefix, "tail": "BBBB"}
        # The fixture's whole point is proving hash-based detection catches
        # a divergence the 200-char preview alone would miss — pin that
        # premise down explicitly so a future payload tweak that breaks it
        # fails loudly here instead of silently testing plain preview
        # divergence instead.
        assert _preview_of(a_payload) == _preview_of(b_payload)
        tracer = _StubTracer(
            {
                "a": [_root_with_complete_output("a", complete_payload=a_payload)],
                "b": [_root_with_complete_output("b", complete_payload=b_payload)],
            }
        )
        diff = await trace_diff("a", "b", tracer=tracer)
        assert diff.comparison_complete is True
        assert diff.a_output_hash != diff.b_output_hash
        assert diff.identical is False

    async def test_comparison_complete_false_for_preview_only_trace(self) -> None:
        payload = {"body": "identical either way"}
        tracer = _StubTracer(
            {
                "a": [_root_with_complete_output("a", complete_payload=payload, preview_only=True)],
                "b": [_root_with_complete_output("b", complete_payload=payload)],
            }
        )
        diff = await trace_diff("a", "b", tracer=tracer)
        assert diff.comparison_complete is False
        assert diff.comparison_incomplete_reason == "preview_only_output"
        assert diff.identical is False

    async def test_comparison_incomplete_for_plain_text_run(self) -> None:
        """A current-format trace with no structured source (e.g. a
        plain-text run) is incomplete for a different, equally stable
        reason than a legacy trace — 'structured_output_unavailable', not
        'preview_only_output'."""
        tracer = _StubTracer(
            {
                "a": [Span(
                    id="a-root", trace_id="a", kind=SpanKind.AGENT_RUN, name="agent",
                    started_at=datetime.now(UTC), ended_at=datetime.now(UTC),
                    duration_ms=10.0, input=None,
                    output={"output": "same text", "scores": None, "output_kind": "text"},
                )],
                "b": [Span(
                    id="b-root", trace_id="b", kind=SpanKind.AGENT_RUN, name="agent",
                    started_at=datetime.now(UTC), ended_at=datetime.now(UTC),
                    duration_ms=10.0, input=None,
                    output={"output": "same text", "scores": None, "output_kind": "text"},
                )],
            }
        )
        diff = await trace_diff("a", "b", tracer=tracer)
        assert diff.comparison_complete is False
        assert diff.comparison_incomplete_reason == "structured_output_unavailable"
        assert diff.identical is False

    async def test_tool_empty_phase_still_compares_complete_output(self) -> None:
        # No TOOL_CALL spans at all — only the AGENT_RUN root. The output
        # comparison must not depend on there being any tool activity.
        a_payload = {"result": "abstain"}
        b_payload = {"result": "abstain"}
        tracer = _StubTracer(
            {
                "a": [_root_with_complete_output("a", complete_payload=a_payload)],
                "b": [_root_with_complete_output("b", complete_payload=b_payload)],
            }
        )
        diff = await trace_diff("a", "b", tracer=tracer)
        assert diff.comparison_complete is True
        assert diff.identical is True

    async def test_trace_diff_json_serializable_without_default_str(self) -> None:
        """Every TraceDiff field — including the new complete-output ones —
        must be directly JSON-serializable so Scout dashboards can
        serialize a diff without a ``default=str`` escape hatch."""
        import dataclasses
        import json as _json

        a_payload = {"result": "abstain"}
        tracer = _StubTracer(
            {
                "a": [_root_with_complete_output("a", complete_payload=a_payload)],
                "b": [_root_with_complete_output("b", complete_payload=a_payload)],
            }
        )
        diff = await trace_diff("a", "b", tracer=tracer)
        serialized = _json.dumps(dataclasses.asdict(diff))
        assert _json.loads(serialized)["comparison_complete"] is True

    async def test_replayability_preserved_alongside_complete_output_capture(self) -> None:
        # replay() must keep working once the root span also carries hash
        # + byte-length evidence — the extra output keys are additive.
        assert replay is not None
