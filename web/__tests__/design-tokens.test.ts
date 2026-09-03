import { describe, it, expect } from "vitest";
import {
  PLATFORM_COLORS,
  VERDICT_COLORS,
  SCORE_COLORS,
  SPAN_KIND_COLORS,
  SPAN_STATUS_COLORS,
  VERB_COLORS,
  getProjectColor,
} from "@/lib/design-tokens";
import { FEEDBACK_VERBS } from "@/types/schema";

const expectThemeSafeBadge = (value: string) => {
  expect(value).toMatch(/(?:^| )bg-/);
  expect(value).toMatch(/(?:^| )text-/);
  expect(value).toMatch(/(?:^| )border-/);
  expect(value).toContain("dark:bg-");
  expect(value).toContain("dark:text-");
  expect(value).toContain("dark:border-");
};

const themeSafeBadgeMaps = [PLATFORM_COLORS, VERDICT_COLORS, SPAN_KIND_COLORS, SPAN_STATUS_COLORS, VERB_COLORS];

describe("theme-safe semantic badges", () => {
  it("provide paired background, foreground, and border treatments", () => {
    themeSafeBadgeMaps.flatMap((tokens) => Object.values(tokens)).forEach(expectThemeSafeBadge);
  });
});

describe("VERDICT_COLORS", () => {
  it("has all expected verdict keys", () => {
    expect(VERDICT_COLORS).toHaveProperty("approve");
    expect(VERDICT_COLORS).toHaveProperty("revise");
    expect(VERDICT_COLORS).toHaveProperty("reject");
    expect(VERDICT_COLORS).toHaveProperty("pending");
  });
});

describe("PLATFORM_COLORS", () => {
  it("has all expected platform keys", () => {
    expect(PLATFORM_COLORS).toHaveProperty("farcaster");
    expect(PLATFORM_COLORS).toHaveProperty("discord");
    expect(PLATFORM_COLORS).toHaveProperty("bluesky");
  });
});

describe("SCORE_COLORS", () => {
  it("has all expected score tier keys", () => {
    expect(SCORE_COLORS).toHaveProperty("high");
    expect(SCORE_COLORS).toHaveProperty("medium");
    expect(SCORE_COLORS).toHaveProperty("low");
  });
});

describe("SPAN_KIND_COLORS", () => {
  it("has expected span kind keys", () => {
    expect(SPAN_KIND_COLORS).toHaveProperty("pipeline_run");
    expect(SPAN_KIND_COLORS).toHaveProperty("pipeline_step");
    expect(SPAN_KIND_COLORS).toHaveProperty("llm_call");
    expect(SPAN_KIND_COLORS).toHaveProperty("tool_call");
    expect(SPAN_KIND_COLORS).toHaveProperty("agent_run");
  });
});

describe("SPAN_STATUS_COLORS", () => {
  it("has expected status keys", () => {
    expect(SPAN_STATUS_COLORS).toHaveProperty("success");
    expect(SPAN_STATUS_COLORS).toHaveProperty("error");
    expect(SPAN_STATUS_COLORS).toHaveProperty("skipped");
  });
});

describe("VERB_COLORS", () => {
  it("has an entry for every FeedbackVerb", () => {
    for (const verb of FEEDBACK_VERBS) {
      expect(VERB_COLORS).toHaveProperty(verb);
      expect(VERB_COLORS[verb]).toContain("bg-");
      expect(VERB_COLORS[verb]).toContain("text-");
      expect(VERB_COLORS[verb]).toContain("border-");
    }
  });
});

describe("getProjectColor", () => {
  it("returns a color string for any project key", () => {
    const color = getProjectColor("gateway");
    expectThemeSafeBadge(color);
  });

  it("returns stable color for the same key", () => {
    const first = getProjectColor("gateway");
    const second = getProjectColor("gateway");
    expect(first).toBe(second);
  });
});
