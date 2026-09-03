// @vitest-environment jsdom

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { DraftCard } from "@/components/organisms/DraftCard";
import type { DraftWithGrade } from "@/types/schema";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function makeDraft(overrides: Partial<DraftWithGrade> = {}): DraftWithGrade {
  return {
    draft_id: 1,
    evaluation_id: 10,
    project_key: "gateway",
    comment_text: "Great post!",
    draft_created_at: "2024-01-01T00:00:00Z",
    verdict: null,
    feedback: null,
    post_id: 1,
    platform: "discord",
    author_name: "alice",
    author_id: "user-alice",
    content: "hello world",
    url: "https://example.com",
    score: 0.8,
    scan_id: 2,
    keyword_route_id: null,
    matched_route: null,
    parent_lookup_status: "not_applicable",
    parent: null,
    relevant: true,
    grade: null,
    ...overrides,
  };
}

describe("DraftCard", () => {
  it("blocks the stable platform author identity", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true } as Response);
    vi.stubGlobal("fetch", fetchMock);
    const { getByRole } = render(
      React.createElement(DraftCard, { draft: makeDraft() })
    );

    fireEvent.click(getByRole("button", { name: /re: alice/i }));
    fireEvent.click(getByRole("button", { name: "Block author" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/settings/blocked-authors",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            platform: "discord",
            author_id: "user-alice",
            author_name: "alice",
            reason: "blocked from Scout review UI",
          }),
        })
      );
    });
  });

  it("routes a human-positive model-negative row through draft generation", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        id: 1,
        evaluation_id: null,
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

    const draft = makeDraft({ relevant: false, comment_text: null });
    const { getByRole, getByPlaceholderText } = render(
      React.createElement(DraftCard, { draft })
    );

    fireEvent.click(getByRole("button", { name: /re: alice/i }));
    fireEvent.click(getByRole("button", { name: /yes/i }));
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
