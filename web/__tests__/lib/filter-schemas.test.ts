import { describe, expect, it } from "vitest";
import {
  parseSearchParams,
  postFiltersSchema,
  ROUTE_ACTIONS,
} from "@/lib/filter-schemas";

describe("repeatable post action filter", () => {
  it.each(ROUTE_ACTIONS)("accepts the RouteAction value %s", (action) => {
    const result = parseSearchParams(
      new URLSearchParams([["action", action]]),
      postFiltersSchema
    );

    expect(result).toEqual({ ok: true, data: { action: [action] } });
  });

  it("preserves repeated action parameters", () => {
    const params = new URLSearchParams();
    params.append("action", "respond");
    params.append("action", "review");

    const result = parseSearchParams(params, postFiltersSchema);

    expect(result).toEqual({
      ok: true,
      data: { action: ["respond", "review"] },
    });
  });

  it("rejects an unknown action", () => {
    const result = parseSearchParams(
      new URLSearchParams([["action", "escalate"]]),
      postFiltersSchema
    );

    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.errors[0]).toContain("action");
  });
});
