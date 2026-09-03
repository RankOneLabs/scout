// @vitest-environment jsdom

import React from "react";
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { MatchedRouteSummary } from "@/components/molecules/MatchedRouteSummary";
import type { MatchedRoute } from "@/types/schema";

function makeRoute(overrides: Partial<MatchedRoute> = {}): MatchedRoute {
  return {
    id: 1,
    project_key: "gw",
    keyword: "alpha",
    match_type: "substring",
    intent: "help people",
    positive_context: ["trusted context"],
    negative_context: ["ignore"],
    evaluate_prompt: "eval_route",
    respond_prompt: "respond_route",
    critique_prompt: "critique_route",
    resolved_prompt_bundle: {
      evaluate: "evaluate body",
      respond: "respond body",
      critique: "critique body",
    },
    ...overrides,
  };
}

describe("MatchedRouteSummary", () => {
  it("renders matched route metadata and prompt bundle details", () => {
    const { getByText } = render(
      React.createElement(MatchedRouteSummary, { route: makeRoute() })
    );

    expect(getByText("Matched route")).toBeTruthy();
    expect(getByText("gw")).toBeTruthy();
    expect(getByText("alpha")).toBeTruthy();
    expect(getByText("Prompt overrides:")).toBeTruthy();
    expect(getByText("evaluate body")).toBeTruthy();
  });
});
