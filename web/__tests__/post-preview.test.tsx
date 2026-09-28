// @vitest-environment jsdom

import React from "react";
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { PostPreview } from "@/components/molecules/PostPreview";
import type { PostWithEvaluation } from "@/types/schema";

afterEach(cleanup);

function post(): PostWithEvaluation {
  return {
    id: 1,
    platform: "discord",
    platform_msg_id: "message-1",
    channel_name: "general",
    channel_id: "channel-1",
    author_name: "alice",
    author_id: "user-1",
    content: "A routed post",
    url: null,
    created_at: "2026-09-01T00:00:00Z",
    scan_id: 1,
    parent_lookup_status: "not_applicable",
    parent: null,
    eval_id: 2,
    relevant: true,
    score: 1,
    reason: "human override",
    relevant_to: ["gateway"],
    keyword_route_id: null,
    matched_route: null,
    relevance_presentation: {
      classifier: "human",
      model: "human",
      action: "respond",
      zeroshot: null,
    },
  };
}

describe("PostPreview", () => {
  it("renders the persisted human action badge", () => {
    const { getByLabelText, queryByText } = render(
      React.createElement(PostPreview, { post: post() })
    );
    expect(getByLabelText("Human action: respond")).toBeTruthy();
    expect(queryByText("100%")).toBeNull();
  });
});
