import { describe, expect, it } from "vitest";
import {
  computeAttemptVerdict,
  computeDecisionMetrics,
  directionalDeltaStatus,
  formatDirectionalDelta,
  mapFailureDetail,
} from "@/lib/experiment-presentation";
import type { ExperimentComparison, ScoreEvidence } from "@/types/feedback-experiments";

function makeComparison(overrides: Partial<ExperimentComparison> = {}): ExperimentComparison {
  return {
    id: 1,
    trace_a_id: "trace-a",
    trace_b_id: "trace-b",
    jig_revision: "rev-abc",
    created_at: "2026-01-01T00:00:00.000000+00:00",
    cost_delta_available: true,
    latency_delta_available: true,
    score_evidence: null,
    trace_diff: {
      trace_a_id: "trace-a",
      trace_b_id: "trace-b",
      tool_divergence: [],
      output_diff: null,
      error_category_change: null,
      score_deltas: {},
      score_details: {},
      cost_delta: 0,
      latency_ms_delta: 0,
      comparison_complete: true,
      comparison_incomplete_reason: null,
      a_output_preview: "",
      b_output_preview: "",
      a_output_hash: null,
      b_output_hash: null,
      a_output_byte_length: null,
      b_output_byte_length: null,
      a_output_complete: null,
      b_output_complete: null,
    },
    domain_diff: {
      baseline: { complete: true, sha256: null, utf8_byte_length: null, incomplete_reason: null },
      candidate: { complete: true, sha256: null, utf8_byte_length: null, incomplete_reason: null },
      grader_not_attached: true,
    },
    ...overrides,
  };
}

function makeScoreEvidence(overrides: Partial<ScoreEvidence> = {}): ScoreEvidence {
  return {
    grader_version: "normalized_edit_distance/v1",
    assembler_version: "assemble_draft_text/v1",
    correction_sha256: "correction-hash",
    reply_revision_id: 42,
    baseline_distance: 0.4,
    candidate_distance: 0.1,
    delta: -0.3,
    grader_attached: true,
    ...overrides,
  };
}

describe("directionalDeltaStatus", () => {
  it("treats a negative delta as better (lower is better)", () => {
    expect(directionalDeltaStatus(-0.3)).toBe("better");
  });

  it("treats a positive delta as worse", () => {
    expect(directionalDeltaStatus(0.3)).toBe("worse");
  });

  it("treats an exact zero delta as a measured 'same', not unavailable", () => {
    expect(directionalDeltaStatus(0)).toBe("same");
  });

  it("treats null as unavailable, distinct from a measured zero", () => {
    expect(directionalDeltaStatus(null)).toBe("unavailable");
  });
});

describe("formatDirectionalDelta", () => {
  const fmt = (n: number) => n.toFixed(2);

  it("formats a negative (better) delta with a minus sign", () => {
    const result = formatDirectionalDelta(-0.3, fmt);
    expect(result.status).toBe("better");
    expect(result.text).toBe("-0.30");
  });

  it("formats a positive (worse) delta with an explicit plus sign", () => {
    const result = formatDirectionalDelta(0.3, fmt);
    expect(result.status).toBe("worse");
    expect(result.text).toBe("+0.30");
  });

  it("formats a zero delta as a measured no-change value, never Unavailable", () => {
    const result = formatDirectionalDelta(0, fmt);
    expect(result.status).toBe("same");
    expect(result.text).toBe("0.00 (no change)");
  });

  it("formats a null (unmeasured) delta as Unavailable, never as a zero", () => {
    const result = formatDirectionalDelta(null, fmt);
    expect(result.status).toBe("unavailable");
    expect(result.text).toBe("Unavailable");
    expect(result.text).not.toMatch(/0/);
  });
});

describe("computeDecisionMetrics", () => {
  it("marks every metric unavailable when there is no persisted comparison", () => {
    for (const metric of computeDecisionMetrics(null)) {
      expect(metric.status).toBe("unavailable");
      expect(metric.text).toBe("Unavailable");
    }
  });

  it("marks cost/latency unavailable (not zero) when the backend flags them unavailable", () => {
    const metrics = computeDecisionMetrics(
      makeComparison({ cost_delta_available: false, latency_delta_available: false })
    );
    expect(metrics.find((m) => m.key === "cost")!.status).toBe("unavailable");
    expect(metrics.find((m) => m.key === "latency")!.status).toBe("unavailable");
  });

  it("reports a real zero cost/latency delta as 'same', not unavailable", () => {
    const metrics = computeDecisionMetrics(makeComparison());
    expect(metrics.find((m) => m.key === "cost")!.status).toBe("same");
    expect(metrics.find((m) => m.key === "latency")!.status).toBe("same");
  });

  it("reports a positive (worse) cost delta distinctly from a negative (better) one", () => {
    const worse = computeDecisionMetrics(
      makeComparison({ trace_diff: { ...makeComparison().trace_diff, cost_delta: 0.01, latency_ms_delta: 0 } })
    );
    const better = computeDecisionMetrics(
      makeComparison({ trace_diff: { ...makeComparison().trace_diff, cost_delta: -0.01, latency_ms_delta: 0 } })
    );
    expect(worse.find((m) => m.key === "cost")!.status).toBe("worse");
    expect(better.find((m) => m.key === "cost")!.status).toBe("better");
  });

  it("derives the correction-distance metric from score_evidence.delta", () => {
    const metrics = computeDecisionMetrics(makeComparison({ score_evidence: makeScoreEvidence() }));
    const correction = metrics.find((m) => m.key === "correction_distance")!;
    expect(correction.status).toBe("better");
    expect(correction.text).toBe("-0.300");
  });

  it("leaves the correction-distance metric unavailable when no grader was attached", () => {
    const metrics = computeDecisionMetrics(makeComparison());
    expect(metrics.find((m) => m.key === "correction_distance")!.status).toBe("unavailable");
  });
});

describe("mapFailureDetail", () => {
  it("passes through a known, allowlisted stage-failure message verbatim", () => {
    const mapped = mapFailureDetail("Candidate phase execution failed before producing a valid result.");
    expect(mapped.known).toBe(true);
    expect(mapped.message).toBe("Candidate phase execution failed before producing a valid result.");
  });

  it("passes through every allowlisted stage message", () => {
    const messages = [
      "The candidate trace could not be verified as a stored AGENT_RUN root.",
      "The candidate's reply-correction score could not be verified or was not produced.",
      "The trace or domain comparison could not be constructed or serialized.",
    ];
    for (const message of messages) {
      expect(mapFailureDetail(message)).toEqual({ known: true, message });
    }
  });

  it("never interpolates an unrecognized persisted error string into the mapped message", () => {
    const mapped = mapFailureDetail("<script>alert(1)</script> unexpected provider payload");
    expect(mapped.known).toBe(false);
    expect(mapped.message).not.toContain("<script>");
    expect(mapped.message).not.toContain("provider payload");
  });

  it("maps a null error_detail to the unknown-failure fallback rather than throwing", () => {
    const mapped = mapFailureDetail(null);
    expect(mapped.known).toBe(false);
  });
});

describe("computeAttemptVerdict", () => {
  it("reports pending for a queued attempt", () => {
    const verdict = computeAttemptVerdict({ status: "queued", error_detail: null, comparison: null });
    expect(verdict.kind).toBe("pending");
    expect(verdict.label).toBe("Queued");
  });

  it("reports pending for a running attempt", () => {
    const verdict = computeAttemptVerdict({ status: "running", error_detail: null, comparison: null });
    expect(verdict.kind).toBe("pending");
    expect(verdict.label).toBe("Running");
  });

  it("reports the mapped failure message for a failed attempt, never the raw persisted text", () => {
    const verdict = computeAttemptVerdict({
      status: "failed",
      error_detail: "unexpected raw provider stack trace",
      comparison: null,
    });
    expect(verdict.kind).toBe("failed");
    expect(verdict.description).not.toContain("provider stack trace");
  });

  it("reports no_comparison for a complete attempt with no persisted comparison", () => {
    const verdict = computeAttemptVerdict({ status: "complete", error_detail: null, comparison: null });
    expect(verdict.kind).toBe("no_comparison");
  });

  it("reports not_graded when the comparison has no score evidence", () => {
    const verdict = computeAttemptVerdict({
      status: "complete",
      error_detail: null,
      comparison: makeComparison(),
    });
    expect(verdict.kind).toBe("not_graded");
  });

  it("recommends the candidate when correction distance improved (negative delta)", () => {
    const verdict = computeAttemptVerdict({
      status: "complete",
      error_detail: null,
      comparison: makeComparison({ score_evidence: makeScoreEvidence({ delta: -0.3 }) }),
    });
    expect(verdict.kind).toBe("candidate_recommended");
  });

  it("does not recommend the candidate when correction distance regressed (positive delta)", () => {
    const verdict = computeAttemptVerdict({
      status: "complete",
      error_detail: null,
      comparison: makeComparison({
        score_evidence: makeScoreEvidence({ baseline_distance: 0.1, candidate_distance: 0.4, delta: 0.3 }),
      }),
    });
    expect(verdict.kind).toBe("candidate_not_recommended");
  });

  it("reports no measurable difference for an exact-zero correction delta, not a recommendation either way", () => {
    const verdict = computeAttemptVerdict({
      status: "complete",
      error_detail: null,
      comparison: makeComparison({
        score_evidence: makeScoreEvidence({ baseline_distance: 0.2, candidate_distance: 0.2, delta: 0 }),
      }),
    });
    expect(verdict.kind).toBe("no_measurable_difference");
  });
});
