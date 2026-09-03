// @vitest-environment jsdom

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { EvaluationCard } from "@/components/organisms/EvaluationCard";
import type { ReviewEvaluation } from "@/types/schema";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function makeEvaluation(overrides: Partial<ReviewEvaluation> = {}): ReviewEvaluation {
  return {
    id: 10,
    post_id: 1,
    relevant: true,
    score: 0.8,
    reason: "on topic",
    relevant_to: ["gateway"],
    scan_id: 2,
    surface_status: "not_relevant",
    failure_reason: null,
    project_key: null,
    keyword_route_id: null,
    matched_route: null,
    post: {
      id: 1,
      platform: "discord",
      platform_msg_id: "msg-1",
      channel_name: "general",
      channel_id: "ch-1",
      author_name: "alice",
      author_id: "u-1",
      content: "hello world",
      url: "https://example.com",
      created_at: "2024-01-01T00:00:00Z",
      scan_id: 2,
      parent_lookup_status: "not_applicable",
      parent: null,
    },
    draft: null,
    critique: null,
    gate_violations: [],
    grade: null,
    ...overrides,
  };
}

describe("EvaluationCard", () => {
  it("passes the evaluation's predicted relevance through to GradeControls so a false negative can be graded", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        id: 1,
        evaluation_id: 10,
        post_id: 1,
        scan_id: 2,
        source: "web",
        graded_at: "2026-01-01T00:00:00Z",
        schema_version: 2,
        needs_regrade: 0,
        relevance_judgment: "false_negative",
        action_judgment: "fail",
        dimensions: ["usefulness"],
        failure_note: "Scout should have surfaced this",
        factual_offending_claim: null,
        factual_disposition: null,
        factual_contradicting_evidence: null,
        context_missing_input: null,
        posture_should_have_been: null,
        implication_implied_claim: null,
        implication_missing_support: null,
      }),
    } as Response);
    vi.stubGlobal("fetch", fetchMock);

    const evaluation = makeEvaluation({ relevant: false });
    const { getByRole, getByPlaceholderText } = render(
      React.createElement(EvaluationCard, { evaluation })
    );

    fireEvent.click(getByRole("button", { name: /re: alice/i }));
    fireEvent.click(getByRole("button", { name: /yes/i }));
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(getByRole("button", { name: "Usefulness" }));
    fireEvent.change(getByPlaceholderText("Failure note..."), {
      target: { value: "Scout should have surfaced this" },
    });
    fireEvent.click(getByRole("button", { name: "Save & generate draft" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/grades/10/promote",
        expect.objectContaining({
          body: JSON.stringify({
            relevance_judgment: "false_negative",
            action_judgment: "fail",
            dimensions: ["usefulness"],
            failure_note: "Scout should have surfaced this",
          }),
        })
      );
    });
  });
});
