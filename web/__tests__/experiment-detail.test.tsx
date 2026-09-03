// @vitest-environment jsdom

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { ExperimentDetail } from "@/components/organisms/ExperimentDetail";
import type { ExperimentDetailResponse } from "@/types/feedback-experiments";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const UNICODE_PROMPT = "You are Scout. Café, 日本語, emoji: 🧠✅";

function makeDetail(overrides: Partial<ExperimentDetailResponse> = {}): ExperimentDetailResponse {
  return {
    id: 1,
    experiment_run: {
      id: 1,
      name: "unicode-exp",
      status: "complete",
      candidate_config: {
        version: 2,
        phase: "relevance",
        model: "candidate-model",
        system_prompt: "candidate prompt",
        system_prompt_sha256: "candidate-hash",
        grader_attached: false,
      },
      created_at: "2026-01-01T00:00:00.000000+00:00",
      completed_at: "2026-01-01T00:01:00.000000+00:00",
    },
    attempt_number: 1,
    supersedes_experiment_id: null,
    status: "complete",
    error_detail: null,
    created_at: "2026-01-01T00:00:00.000000+00:00",
    completed_at: "2026-01-01T00:01:00.000000+00:00",
    baseline: {
      phase_run_id: 10,
      phase: "relevance",
      trace_id: "trace-baseline-1",
      model: "baseline-model",
      system_prompt: UNICODE_PROMPT,
      system_prompt_sha256: "baseline-hash",
      phase_run_url: "/feedback/phase-runs/10",
      trace_url: "/traces/trace-baseline-1",
    },
    baseline_evidence: {
      version: 2,
      recorded_input_sha256: "input-hash",
      baseline_prompt_reused: false,
    },
    evaluation_id: 5,
    snapshot: {
      snapshot_phase_id: 1,
      snapshot_id: 1,
      phase: "relevance",
      policy_version: "v1",
      lookback_days: 14,
      max_grades: 50,
      snapshot_url: "/feedback?snapshotId=1",
    },
    candidate: {
      trace_id: "trace-candidate-1",
      trace_url: "/traces/trace-candidate-1",
      model: "candidate-model",
      system_prompt: "candidate prompt",
      system_prompt_sha256: "candidate-hash",
      llm_call_count: 3,
      cost: 0.02,
    },
    comparison: {
      id: 1,
      trace_a_id: "trace-baseline-1",
      trace_b_id: "trace-candidate-1",
      jig_revision: "rev-abc",
      created_at: "2026-01-01T00:01:00.000000+00:00",
      cost_delta_available: true,
      latency_delta_available: true,
      score_evidence: null,
      trace_diff: {
        trace_a_id: "trace-baseline-1",
        trace_b_id: "trace-candidate-1",
        tool_divergence: [],
        output_diff: null,
        error_category_change: null,
        score_deltas: {},
        score_details: {},
        cost_delta: 0.01,
        latency_ms_delta: 100,
        comparison_complete: true,
        comparison_incomplete_reason: null,
        a_output_preview: "",
        b_output_preview: "",
        a_output_hash: "hash-a",
        b_output_hash: "hash-b",
        a_output_byte_length: 10,
        b_output_byte_length: 12,
        a_output_complete: { relevant: true },
        b_output_complete: { relevant: true },
      },
      domain_diff: {
        baseline: { complete: true, sha256: "sha-a", utf8_byte_length: 10, incomplete_reason: null, value: { relevant: true } },
        candidate: { complete: true, sha256: "sha-b", utf8_byte_length: 12, incomplete_reason: null, value: { relevant: true, note: "café" } },
        grader_not_attached: true,
        additions: ["/note"],
        removals: [],
        changes: [],
      },
    },
    ...overrides,
  };
}

function mockFetchOnce(body: unknown, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("ExperimentDetail", () => {
  it("keeps prompt disclosures collapsed by default", async () => {
    mockFetchOnce(makeDetail());
    const { container } = render(React.createElement(ExperimentDetail, { experimentId: 1 }));

    await waitFor(() => screen.getByText("unicode-exp"));

    const detailsElements = container.querySelectorAll("details");
    expect(detailsElements.length).toBeGreaterThan(0);
    for (const el of Array.from(detailsElements)) {
      expect(el.hasAttribute("open")).toBe(false);
    }
  });

  it("preserves unicode content in the (collapsed) system prompt disclosure", async () => {
    mockFetchOnce(makeDetail());
    const { container } = render(React.createElement(ExperimentDetail, { experimentId: 1 }));

    await waitFor(() => screen.getByText("unicode-exp"));

    expect(container.textContent).toContain("Café");
    expect(container.textContent).toContain("日本語");
    expect(container.textContent).toContain("🧠");
    expect(container.textContent).toContain("café");
  });

  it("contains no mutation controls: no form, no submit/POST-triggering button", async () => {
    mockFetchOnce(makeDetail());
    const { container } = render(React.createElement(ExperimentDetail, { experimentId: 1 }));

    await waitFor(() => screen.getByText("unicode-exp"));

    expect(container.querySelector("form")).toBeNull();
    expect(container.querySelectorAll("button").length).toBe(0);
    expect(container.querySelectorAll("input").length).toBe(0);
  });

  it("renders Unavailable for null candidate trace/comparison rather than coercing to empty/zero", async () => {
    mockFetchOnce(
      makeDetail({
        status: "queued",
        candidate: {
          trace_id: null,
          trace_url: null,
          model: "candidate-model",
          system_prompt: "candidate prompt",
          system_prompt_sha256: "candidate-hash",
          llm_call_count: null,
          cost: null,
        },
        comparison: null,
      })
    );
    const { container } = render(React.createElement(ExperimentDetail, { experimentId: 1 }));

    await waitFor(() => screen.getByText("unicode-exp"));

    const unavailableCount = Array.from(container.querySelectorAll("span")).filter(
      (el) => el.textContent === "Unavailable"
    ).length;
    expect(unavailableCount).toBeGreaterThan(0);
    expect(container.textContent).not.toMatch(/\$0\.0000/);
    expect(screen.queryByText(/No comparison has been persisted yet/)).not.toBeNull();
  });

  it("shows the mapped message for a known, allowlisted failure", async () => {
    mockFetchOnce(
      makeDetail({
        status: "failed",
        error_detail: "Candidate phase execution failed before producing a valid result.",
        comparison: null,
      })
    );
    render(React.createElement(ExperimentDetail, { experimentId: 1 }));

    await waitFor(() => screen.getByText("Candidate phase execution failed before producing a valid result."));
  });

  it("never renders an unrecognized persisted error_detail string, even escaped", async () => {
    const rawText = "<script>alert(1)</script> unexpected raw provider stack trace";
    mockFetchOnce(makeDetail({ status: "failed", error_detail: rawText, comparison: null }));
    const { container } = render(React.createElement(ExperimentDetail, { experimentId: 1 }));

    await waitFor(() => screen.getByText("unicode-exp"));

    expect(container.innerHTML).not.toContain("unexpected raw provider stack trace");
    expect(container.textContent).not.toContain("unexpected raw provider stack trace");
    expect(
      screen.getByText("This attempt failed for a reason outside the known failure categories.")
    ).toBeTruthy();
  });

  it("renders unavailable deltas and complete tool-call evidence without inventing zero", async () => {
    const detail = makeDetail();
    detail.comparison!.cost_delta_available = false;
    detail.comparison!.latency_delta_available = false;
    detail.comparison!.trace_diff.score_details = { quality: [0.8, null] };
    detail.comparison!.trace_diff.score_deltas = {};
    detail.comparison!.trace_diff.tool_divergence = [
      {
        index: 0,
        divergence: "output",
        tier: "identity",
        index_a: 2,
        index_b: 3,
        a: {
          name: "lookup_record",
          args: { record_id: 17 },
          output: "baseline output",
          error: null,
        },
        b: {
          name: "lookup_record",
          args: { record_id: 18 },
          output: null,
          error: "candidate tool failed",
        },
      },
    ];
    mockFetchOnce(detail);
    const { container } = render(React.createElement(ExperimentDetail, { experimentId: 1 }));

    await waitFor(() => screen.getByText("unicode-exp"));

    // Decision-metric header cards (the primary, decision-first surface).
    expect(screen.getByText("Cost delta").closest("div")?.textContent).toContain("Unavailable");
    expect(screen.getByText("Latency delta").closest("div")?.textContent).toContain("Unavailable");
    // Raw forensic values inside the Comparison disclosure.
    expect(screen.getByText("Raw cost delta").parentElement?.textContent).toContain("Unavailable");
    expect(screen.getByText("Raw latency delta (ms)").parentElement?.textContent).toContain(
      "Unavailable"
    );
    expect(container.textContent).not.toContain("0 (no change)");
    expect(container.textContent).toContain('"record_id": 17');
    expect(container.textContent).toContain("baseline output");
    expect(container.textContent).toContain("candidate tool failed");
    expect(container.textContent).toContain("trace index 2");
    expect(container.textContent).toContain("trace index 3");
  });

  it("renders the pinned correction oracle and score evidence for a graded reply_draft attempt", async () => {
    const detail = makeDetail({
      attempt_number: 2,
      supersedes_experiment_id: 7,
      experiment_run: {
        id: 1,
        name: "unicode-exp",
        status: "complete",
        candidate_config: {
          version: 2,
          phase: "reply_draft",
          model: "candidate-model",
          system_prompt: "candidate prompt",
          system_prompt_sha256: "candidate-hash",
          grader_attached: true,
        },
        created_at: "2026-01-01T00:00:00.000000+00:00",
        completed_at: "2026-01-01T00:01:00.000000+00:00",
      },
      baseline_evidence: {
        version: 2,
        recorded_input_sha256: "input-hash",
        baseline_prompt_reused: true,
        baseline_model: "baseline-model",
        baseline_prompt_sha256: "baseline-hash",
        reply_revision_id: 42,
        correction_sha256: "correction-hash",
        project_key: "gateway",
        dossier_summary_id: "gateway-dossier",
        dossier_revision: "a".repeat(40),
        grader_version: "normalized_edit_distance/v1",
        assembler_version: "assemble_draft_text/v1",
      },
    });
    detail.comparison!.score_evidence = {
      grader_version: "normalized_edit_distance/v1",
      assembler_version: "assemble_draft_text/v1",
      correction_sha256: "correction-hash",
      reply_revision_id: 42,
      baseline_distance: 0.4,
      candidate_distance: 0.1,
      delta: -0.3,
      grader_attached: true,
    };
    mockFetchOnce(detail);
    const { container } = render(React.createElement(ExperimentDetail, { experimentId: 1 }));

    await waitFor(() => screen.getByText("unicode-exp"));

    expect(container.textContent).toContain("attempt 2 of run #1");
    expect(container.textContent).toContain("retry of");
    expect(container.textContent).toContain("#7");
    expect(container.textContent).toContain("gateway-dossier");
    expect(container.textContent).toContain("normalized_edit_distance/v1");
    expect(container.textContent).toContain("-0.3");
    expect(container.textContent).toContain(
      "the candidate is closer to the pinned correction than the baseline was"
    );
    // Decision-first: the verdict leads the page and recommends the candidate.
    expect(screen.getByText("Candidate recommended")).toBeTruthy();
  });

  it("renders 'no correction oracle' and 'no grader attached' for an ungraded attempt", async () => {
    mockFetchOnce(makeDetail());
    const { container } = render(React.createElement(ExperimentDetail, { experimentId: 1 }));

    await waitFor(() => screen.getByText("unicode-exp"));

    expect(container.textContent).toContain("No correction oracle was pinned");
    expect(container.textContent).toContain("No reply-correction grader was attached");
    expect(screen.getByText("Not graded")).toBeTruthy();
  });

  it("leads with a Verdict section ahead of the collapsed supporting-evidence disclosures", async () => {
    mockFetchOnce(makeDetail());
    const { container } = render(React.createElement(ExperimentDetail, { experimentId: 1 }));

    await waitFor(() => screen.getByText("unicode-exp"));

    const verdictSection = container.querySelector('[aria-label="Verdict"]');
    expect(verdictSection).not.toBeNull();
    const firstDisclosure = container.querySelector("details");
    expect(firstDisclosure).not.toBeNull();
    // Verdict precedes every collapsible disclosure in document order.
    expect(
      verdictSection!.compareDocumentPosition(firstDisclosure!) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    expect(within(verdictSection as HTMLElement).getByText("Correction distance")).toBeTruthy();
    expect(within(verdictSection as HTMLElement).getByText("Cost delta")).toBeTruthy();
    expect(within(verdictSection as HTMLElement).getByText("Latency delta")).toBeTruthy();
  });

  it("does not recommend the candidate when correction distance regressed (positive delta)", async () => {
    const detail = makeDetail();
    detail.comparison!.score_evidence = {
      grader_version: "normalized_edit_distance/v1",
      assembler_version: "assemble_draft_text/v1",
      correction_sha256: "correction-hash",
      reply_revision_id: 42,
      baseline_distance: 0.1,
      candidate_distance: 0.4,
      delta: 0.3,
      grader_attached: true,
    };
    mockFetchOnce(detail);
    render(React.createElement(ExperimentDetail, { experimentId: 1 }));

    await waitFor(() => screen.getByText("unicode-exp"));

    expect(screen.getByText("Candidate not recommended")).toBeTruthy();
  });

  it("reports no measurable difference for an exact-zero correction distance delta", async () => {
    const detail = makeDetail();
    detail.comparison!.score_evidence = {
      grader_version: "normalized_edit_distance/v1",
      assembler_version: "assemble_draft_text/v1",
      correction_sha256: "correction-hash",
      reply_revision_id: 42,
      baseline_distance: 0.2,
      candidate_distance: 0.2,
      delta: 0,
      grader_attached: true,
    };
    mockFetchOnce(detail);
    render(React.createElement(ExperimentDetail, { experimentId: 1 }));

    await waitFor(() => screen.getByText("unicode-exp"));

    expect(screen.getByText("No measurable difference")).toBeTruthy();
    expect(screen.getByText("0.000 (no change)")).toBeTruthy();
  });
});
