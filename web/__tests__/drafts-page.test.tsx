// @vitest-environment jsdom

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import type { DraftWithGrade, ReviewEvaluation } from "@/types/schema";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.resetModules();
});

function makeDraft(overrides: Partial<DraftWithGrade>): DraftWithGrade {
  return {
    draft_id: 1,
    evaluation_id: 200,
    project_key: null,
    comment_text: null,
    draft_created_at: "2026-05-15T00:00:00Z",
    verdict: null,
    feedback: null,
    post_id: 10,
    platform: "discord",
    author_name: "alice",
    author_id: "user-alice",
    content: "hello world",
    url: null,
    score: 0.8,
    scan_id: 7,
    keyword_route_id: null,
    matched_route: null,
    parent_lookup_status: "not_applicable",
    parent: null,
    relevant: true,
    grade: null,
    ...overrides,
  };
}

// Two drafts sharing post_id (the same post surfaced across two scans)
// but carrying distinct evaluation_ids — the exact G-008 rescan shape.
const draftA = makeDraft({ draft_id: 1, evaluation_id: 200, author_name: "alice" });
const draftB = makeDraft({ draft_id: 2, evaluation_id: 201, author_name: "bob" });
const negativeCase: ReviewEvaluation = {
  id: 300,
  post_id: 30,
  relevant: false,
  score: 0.2,
  reason: "not relevant",
  relevant_to: [],
  scan_id: 8,
  surface_status: "not_relevant",
  failure_reason: null,
  project_key: "agent-ops",
  posture: null,
  dossier_revision: null,
  dossier_summary_id: null,
  keyword_route_id: null,
  matched_route: null,
  post: {
    id: 30,
    platform: "bluesky",
    platform_msg_id: "negative-30",
    channel_name: null,
    channel_id: null,
    author_name: "carol",
    author_id: "user-carol",
    content: "a skipped post",
    url: null,
    created_at: "2026-05-15T00:00:00Z",
    scan_id: 8,
    parent_lookup_status: "not_applicable",
    parent: null,
  },
  draft: null,
  critique: null,
  gate_violations: [],
  grade: null,
};

vi.mock("@/hooks/use-drafts", () => ({
  useDrafts: () => ({
    drafts: [draftA, draftB],
    filters: {},
    setFilters: () => {},
    loading: false,
    error: null,
    hasMore: false,
    loadMore: () => {},
    isLoadingMore: false,
  }),
}));

vi.mock("@/hooks/use-negative-grading-cases", () => ({
  useNegativeGradingCases: () => ({
    evaluations: [negativeCase],
    loading: false,
    error: null,
    hasMore: false,
    loadMore: () => {},
    isLoadingMore: false,
  }),
}));

function gradeResponse(evaluationId: number) {
  return {
    id: evaluationId,
    evaluation_id: evaluationId,
    post_id: 10,
    scan_id: 7,
    source: "web",
    graded_at: "2026-05-15T00:00:00.000Z",
    schema_version: 2,
    needs_regrade: 0,
    relevance_judgment: "correct",
    action_judgment: "accept",
    dimensions: null,
    failure_note: null,
    factual_offending_claim: null,
    factual_disposition: null,
    factual_contradicting_evidence: null,
    context_missing_input: null,
    posture_should_have_been: null,
    implication_implied_claim: null,
    implication_missing_support: null,
  };
}

describe("DraftsPage grade overlay", () => {
  it("presents Drafts and Negative Cases as clickable sections under Grading", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [],
    } as Response));

    const { default: GradingPage } = await import("@/app/drafts/page");
    const { getByRole, getByText } = render(React.createElement(GradingPage));

    expect(getByRole("heading", { name: "Grading" })).toBeTruthy();
    const draftsButton = getByRole("button", { name: "Drafts" });
    const negativeCasesButton = getByRole("button", { name: "Negative Cases" });
    expect(draftsButton.getAttribute("aria-pressed")).toBe("true");
    expect(negativeCasesButton.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(negativeCasesButton);
    expect(draftsButton.getAttribute("aria-pressed")).toBe("false");
    expect(negativeCasesButton.getAttribute("aria-pressed")).toBe("true");
    expect(getByText("Model-negative cases needing review")).toBeTruthy();
    expect(getByRole("button", { name: /re: carol/i })).toBeTruthy();
  });

  it("updates only the graded evaluation's draft when two drafts share a post_id", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (typeof url === "string" && url.startsWith("/api/grades/200")) {
        return Promise.resolve({
          ok: true,
          json: async () => gradeResponse(200),
        } as Response);
      }
      // /api/projects and any other incidental call.
      return Promise.resolve({ ok: true, json: async () => [] } as Response);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { default: DraftsPage } = await import("@/app/drafts/page");
    const { getAllByRole } = render(React.createElement(DraftsPage));

    const expandButtons = getAllByRole("button", { name: /re: (alice|bob)/i });
    fireEvent.click(expandButtons[0]); // alice / evaluation 200
    fireEvent.click(expandButtons[1]); // bob / evaluation 201

    const yesButtons = getAllByRole("button", { name: /^yes$/i });
    fireEvent.click(yesButtons[0]);
    const looksGoodButtons = getAllByRole("button", { name: /looks good/i });
    fireEvent.click(looksGoodButtons[0]);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/grades/200",
        expect.objectContaining({ method: "POST" })
      );
    });

    await waitFor(() => {
      expect(getAllByRole("button", { name: /^yes$/i })[0].className).toMatch(/border-green/);
    });
    // The other draft (evaluation 201) must remain ungraded.
    expect(getAllByRole("button", { name: /^yes$/i })[1].className).not.toMatch(/border-green/);
  });
});
