import { describe, it, expect } from "vitest";
import {
  getSpanStatus,
  formatDurationMs,
  buildSpanTree,
} from "@/lib/transforms";
import type { TraceSpan } from "@/types/schema";

function makeSpan(overrides: Partial<TraceSpan> = {}): TraceSpan {
  return {
    id: "span-1",
    trace_id: "trace-1",
    parent_id: null,
    kind: "pipeline_run",
    name: "test-pipeline",
    input: null,
    output: null,
    started_at: "2024-01-01T00:00:00Z",
    ended_at: "2024-01-01T00:00:01Z",
    duration_ms: 1000,
    metadata: null,
    error: null,
    usage_input_tokens: null,
    usage_output_tokens: null,
    usage_cost: null,
    ...overrides,
  };
}

describe("getSpanStatus", () => {
  it("returns 'success' for completed span without error", () => {
    expect(getSpanStatus(makeSpan())).toBe("success");
  });

  it("returns 'error' when span has error", () => {
    expect(getSpanStatus(makeSpan({ error: "boom" }))).toBe("error");
  });

  it("returns 'skipped' when ended_at is null and no error", () => {
    expect(getSpanStatus(makeSpan({ ended_at: null }))).toBe("skipped");
  });

  it("returns 'error' when both error and ended_at are set", () => {
    expect(
      getSpanStatus(makeSpan({ error: "fail", ended_at: "2024-01-01T00:00:02Z" }))
    ).toBe("error");
  });
});

describe("formatDurationMs", () => {
  it("returns dash for null", () => {
    expect(formatDurationMs(null)).toBe("—");
  });

  it("formats sub-second as milliseconds", () => {
    expect(formatDurationMs(250)).toBe("250ms");
  });

  it("formats exactly 1 second", () => {
    expect(formatDurationMs(1000)).toBe("1.0s");
  });

  it("formats multi-second", () => {
    expect(formatDurationMs(1250)).toBe("1.3s");
  });

  it("formats zero as 0ms", () => {
    expect(formatDurationMs(0)).toBe("0ms");
  });
});

describe("buildSpanTree", () => {
  it("identifies root spans (parent_id null)", () => {
    const spans = [
      makeSpan({ id: "root", parent_id: null }),
      makeSpan({ id: "child", parent_id: "root" }),
    ];
    const tree = buildSpanTree(spans);
    expect(tree).toHaveLength(1);
    expect(tree[0].span.id).toBe("root");
  });

  it("groups children under parent", () => {
    const spans = [
      makeSpan({ id: "root", parent_id: null }),
      makeSpan({ id: "child-1", parent_id: "root" }),
      makeSpan({ id: "child-2", parent_id: "root" }),
    ];
    const tree = buildSpanTree(spans);
    expect(tree[0].children).toHaveLength(2);
  });

  it("nests grandchildren correctly", () => {
    const spans = [
      makeSpan({ id: "root", parent_id: null }),
      makeSpan({ id: "child", parent_id: "root" }),
      makeSpan({ id: "grandchild", parent_id: "child" }),
    ];
    const tree = buildSpanTree(spans);
    expect(tree[0].children[0].children).toHaveLength(1);
    expect(tree[0].children[0].children[0].span.id).toBe("grandchild");
  });

  it("returns empty array for empty input", () => {
    expect(buildSpanTree([])).toEqual([]);
  });
});
