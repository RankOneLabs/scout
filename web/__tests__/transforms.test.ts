import { describe, it, expect } from "vitest";
import {
  groupBy,
  groupByPlatform,
  groupByVerdict,
  classifyScore,
  formatDuration,
  truncateContent,
  formatScore,
  formatPercentage,
  formatTimestamp,
  formatRelativeDays,
  groupByProject,
} from "@/lib/transforms";
import type {
  MatchedRoute,
  PostWithEvaluation,
  DraftWithContext,
} from "@/types/schema";

function makeRoute(overrides: Partial<MatchedRoute> = {}): MatchedRoute {
  return {
    id: 1,
    project_key: "gateway",
    keyword: "gateway",
    match_type: "substring",
    intent: "help users",
    positive_context: ["trusted"],
    negative_context: ["ignore"],
    evaluate_prompt: "eval_prompt",
    respond_prompt: "respond_prompt",
    critique_prompt: "critique_prompt",
    resolved_prompt_bundle: {
      evaluate: "evaluate body",
      respond: "respond body",
      critique: "critique body",
    },
    ...overrides,
  };
}

function makePost(overrides: Partial<PostWithEvaluation> = {}): PostWithEvaluation {
  return {
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
    scan_id: 1,
    parent_lookup_status: "not_applicable",
    parent: null,
    eval_id: 1,
    relevant: true,
    score: 0.8,
    reason: "relevant",
    relevant_to: ["gateway"],
    keyword_route_id: 1,
    matched_route: makeRoute(),
    ...overrides,
  };
}

function makeDraft(overrides: Partial<DraftWithContext> = {}): DraftWithContext {
  return {
    draft_id: 1,
    project_key: "gateway",
    comment_text: "Great post!",
    draft_created_at: "2024-01-01T00:00:00Z",
    verdict: "approve",
    feedback: "Looks good",
    post_id: 1,
    platform: "discord",
    author_name: "alice",
    author_id: "user-alice",
    content: "hello world",
    url: "https://example.com",
    score: 0.8,
    scan_id: 1,
    keyword_route_id: 1,
    matched_route: makeRoute(),
    parent_lookup_status: "not_applicable",
    parent: null,
    relevant: true,
    ...overrides,
  };
}

describe("groupBy", () => {
  it("groups items by an arbitrary key function", () => {
    const items = [
      { name: "alice", team: "red" },
      { name: "bob", team: "blue" },
      { name: "carol", team: "red" },
    ];
    const result = groupBy(items, (item) => item.team);
    expect(result.get("red")?.map((i) => i.name)).toEqual(["alice", "carol"]);
    expect(result.get("blue")?.map((i) => i.name)).toEqual(["bob"]);
  });

  it("supports non-string key types", () => {
    const items = [1, 2, 3, 4, 5];
    const result = groupBy(items, (n) => n % 2 === 0);
    expect(result.get(true)).toEqual([2, 4]);
    expect(result.get(false)).toEqual([1, 3, 5]);
  });

  it("preserves insertion order within each group", () => {
    const items = [
      { id: 1, key: "a" },
      { id: 2, key: "b" },
      { id: 3, key: "a" },
      { id: 4, key: "a" },
      { id: 5, key: "b" },
    ];
    const result = groupBy(items, (item) => item.key);
    expect(result.get("a")?.map((i) => i.id)).toEqual([1, 3, 4]);
    expect(result.get("b")?.map((i) => i.id)).toEqual([2, 5]);
  });

  it("returns empty map for empty input", () => {
    const result = groupBy<number, string>([], () => "whatever");
    expect(result.size).toBe(0);
  });

  it("collapses to one group when all items share a key", () => {
    const items = [10, 20, 30];
    const result = groupBy(items, () => "same");
    expect(result.size).toBe(1);
    expect(result.get("same")).toEqual([10, 20, 30]);
  });
});

describe("groupByPlatform", () => {
  it("groups posts by platform", () => {
    const posts = [
      makePost({ id: 1, platform: "discord" }),
      makePost({ id: 2, platform: "farcaster" }),
      makePost({ id: 3, platform: "discord" }),
    ];
    const result = groupByPlatform(posts);
    expect(result.get("discord")?.length).toBe(2);
    expect(result.get("farcaster")?.length).toBe(1);
  });

  it("returns empty map for empty input", () => {
    const result = groupByPlatform([]);
    expect(result.size).toBe(0);
  });
});

describe("groupByVerdict", () => {
  it("groups drafts by verdict", () => {
    const drafts = [
      makeDraft({ draft_id: 1, verdict: "approve" }),
      makeDraft({ draft_id: 2, verdict: "reject" }),
      makeDraft({ draft_id: 3, verdict: "approve" }),
      makeDraft({ draft_id: 4, verdict: "revise" }),
    ];
    const result = groupByVerdict(drafts);
    expect(result.get("approve")?.length).toBe(2);
    expect(result.get("reject")?.length).toBe(1);
    expect(result.get("revise")?.length).toBe(1);
  });

  it("uses 'pending' for null critique", () => {
    const drafts = [makeDraft({ draft_id: 1, verdict: null })];
    const result = groupByVerdict(drafts);
    expect(result.get("pending")?.length).toBe(1);
  });
});

describe("groupByProject", () => {
  it("groups drafts by project_key", () => {
    const drafts = [
      makeDraft({ draft_id: 1, project_key: "gateway" }),
      makeDraft({ draft_id: 2, project_key: "zk-extension" }),
      makeDraft({ draft_id: 3, project_key: "gateway" }),
    ];
    const result = groupByProject(drafts);
    expect(result.get("gateway")?.length).toBe(2);
    expect(result.get("zk-extension")?.length).toBe(1);
  });
});

describe("classifyScore", () => {
  it("returns 'high' for >= 0.7", () => {
    expect(classifyScore(0.7)).toBe("high");
    expect(classifyScore(1.0)).toBe("high");
    expect(classifyScore(0.85)).toBe("high");
  });

  it("returns 'medium' for >= 0.3 and < 0.7", () => {
    expect(classifyScore(0.3)).toBe("medium");
    expect(classifyScore(0.5)).toBe("medium");
    expect(classifyScore(0.69)).toBe("medium");
  });

  it("returns 'low' for < 0.3", () => {
    expect(classifyScore(0.0)).toBe("low");
    expect(classifyScore(0.29)).toBe("low");
  });
});

describe("formatDuration", () => {
  it("formats normal duration", () => {
    const start = "2024-01-01T00:00:00Z";
    const end = "2024-01-01T00:02:30Z";
    expect(formatDuration(start, end)).toBe("2m 30s");
  });

  it("formats short duration in seconds", () => {
    const start = "2024-01-01T00:00:00Z";
    const end = "2024-01-01T00:00:45Z";
    expect(formatDuration(start, end)).toBe("45s");
  });

  it("returns 'In progress' when end is null", () => {
    expect(formatDuration("2024-01-01T00:00:00Z", null)).toBe("In progress");
  });
});

describe("truncateContent", () => {
  it("returns short content unchanged", () => {
    expect(truncateContent("hello", 100)).toBe("hello");
  });

  it("truncates long content with ellipsis", () => {
    const long = "a".repeat(200);
    const result = truncateContent(long, 50);
    expect(result.length).toBe(51); // 50 + ellipsis char
    expect(result.endsWith("…")).toBe(true);
  });
});

describe("formatScore", () => {
  it("formats score as percentage", () => {
    expect(formatScore(0.85)).toBe("85%");
    expect(formatScore(1.0)).toBe("100%");
    expect(formatScore(0)).toBe("0%");
  });

  it("returns dash for null", () => {
    expect(formatScore(null)).toBe("—");
  });
});

describe("formatPercentage", () => {
  it("formats value with one decimal", () => {
    expect(formatPercentage(85.5)).toBe("85.5%");
    expect(formatPercentage(0)).toBe("0.0%");
  });
});

describe("formatTimestamp", () => {
  it("returns dash for null", () => {
    expect(formatTimestamp(null)).toBe("—");
  });

  it("formats valid ISO string", () => {
    const result = formatTimestamp("2024-06-15T14:30:00Z");
    expect(result).toContain("Jun");
    expect(result).toContain("15");
  });
});

describe("formatRelativeDays", () => {
  it("returns 'due today' when target and now share the calendar day in UTC", () => {
    expect(
      formatRelativeDays("2026-05-18T03:00:00Z", "2026-05-18T20:00:00Z"),
    ).toBe("due today");
  });

  it("returns 'N days from now' for future targets", () => {
    expect(
      formatRelativeDays("2026-05-23T00:00:00Z", "2026-05-18T00:00:00Z"),
    ).toBe("5 days from now");
  });

  it("returns 'N days overdue' for past targets", () => {
    expect(
      formatRelativeDays("2026-05-15T00:00:00Z", "2026-05-18T00:00:00Z"),
    ).toBe("3 days overdue");
  });

  it("counts calendar days regardless of intra-day hour offsets", () => {
    expect(
      formatRelativeDays("2026-05-19T01:00:00Z", "2026-05-18T23:00:00Z"),
    ).toBe("1 day from now");
  });

  it("singularises 'day' when the magnitude is exactly 1", () => {
    expect(
      formatRelativeDays("2026-05-17T00:00:00Z", "2026-05-18T00:00:00Z"),
    ).toBe("1 day overdue");
  });
});
