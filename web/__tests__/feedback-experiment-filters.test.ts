import { describe, expect, it } from "vitest";
import {
  cursorMatchesFilters,
  decodeExperimentListCursor,
  encodeExperimentListCursor,
  parseExperimentListSearchParams,
} from "@/lib/feedback-experiment-filters";

describe("parseExperimentListSearchParams", () => {
  it("accepts a well-formed filter combination", () => {
    const result = parseExperimentListSearchParams(
      new URLSearchParams("status=complete&phase=relevance&limit=25")
    );
    expect(result).toEqual({
      ok: true,
      data: { status: "complete", phase: "relevance", limit: 25 },
    });
  });

  it("accepts no filters at all", () => {
    const result = parseExperimentListSearchParams(new URLSearchParams(""));
    expect(result).toEqual({ ok: true, data: {} });
  });

  it("rejects an unknown status value", () => {
    const result = parseExperimentListSearchParams(new URLSearchParams("status=bogus"));
    expect(result.ok).toBe(false);
  });

  it("rejects an unknown phase value", () => {
    const result = parseExperimentListSearchParams(new URLSearchParams("phase=drafting"));
    expect(result.ok).toBe(false);
  });

  it("rejects an empty status value rather than treating it as absent", () => {
    const result = parseExperimentListSearchParams(new URLSearchParams("status="));
    expect(result).toEqual({ ok: false, errors: ["status: must not be empty"] });
  });

  it("rejects a repeated status value rather than last-value-wins", () => {
    const params = new URLSearchParams();
    params.append("status", "queued");
    params.append("status", "running");
    const result = parseExperimentListSearchParams(params);
    expect(result).toEqual({ ok: false, errors: ["status: must not be repeated"] });
  });

  it("rejects limit <= 0 and limit > 100", () => {
    expect(parseExperimentListSearchParams(new URLSearchParams("limit=0")).ok).toBe(false);
    expect(parseExperimentListSearchParams(new URLSearchParams("limit=101")).ok).toBe(false);
  });

  it("rejects a non-integer limit", () => {
    expect(parseExperimentListSearchParams(new URLSearchParams("limit=12.5")).ok).toBe(false);
  });
});

describe("experiment list cursor codec", () => {
  it("round-trips through encode/decode with null filters", () => {
    const cursor = { created_at: "2026-01-01T00:00:00.000000+00:00", id: 7, status: null, phase: null };
    const encoded = encodeExperimentListCursor(cursor);
    expect(decodeExperimentListCursor(encoded)).toEqual(cursor);
  });

  it("round-trips with bound status/phase filters", () => {
    const cursor = {
      created_at: "2026-01-01T00:00:00.000000+00:00",
      id: 3,
      status: "complete" as const,
      phase: "critic" as const,
    };
    const encoded = encodeExperimentListCursor(cursor);
    expect(decodeExperimentListCursor(encoded)).toEqual(cursor);
  });

  it("returns null for malformed base64", () => {
    expect(decodeExperimentListCursor("not-valid-base64!!")).toBeNull();
  });

  it("returns null for a cursor missing the filter fields", () => {
    const raw = Buffer.from(JSON.stringify({ created_at: "2026-01-01T00:00:00Z", id: 1 })).toString(
      "base64url"
    );
    expect(decodeExperimentListCursor(raw)).toBeNull();
  });

  it("returns null for an invalid created_at", () => {
    const raw = Buffer.from(
      JSON.stringify({ created_at: "not-a-date", id: 1, status: null, phase: null })
    ).toString("base64url");
    expect(decodeExperimentListCursor(raw)).toBeNull();
  });

  it("returns null for a non-positive id", () => {
    const raw = Buffer.from(
      JSON.stringify({ created_at: "2026-01-01T00:00:00Z", id: 0, status: null, phase: null })
    ).toString("base64url");
    expect(decodeExperimentListCursor(raw)).toBeNull();
  });

  it("returns null for an out-of-allowlist status", () => {
    const raw = Buffer.from(
      JSON.stringify({ created_at: "2026-01-01T00:00:00Z", id: 1, status: "bogus", phase: null })
    ).toString("base64url");
    expect(decodeExperimentListCursor(raw)).toBeNull();
  });
});

describe("cursorMatchesFilters", () => {
  it("matches when both null", () => {
    expect(
      cursorMatchesFilters({ created_at: "x", id: 1, status: null, phase: null }, {})
    ).toBe(true);
  });

  it("matches when bound filters agree with the request", () => {
    expect(
      cursorMatchesFilters(
        { created_at: "x", id: 1, status: "complete", phase: "critic" },
        { status: "complete", phase: "critic" }
      )
    ).toBe(true);
  });

  it("rejects when the cursor's bound status differs from the request", () => {
    expect(
      cursorMatchesFilters(
        { created_at: "x", id: 1, status: "complete", phase: null },
        { status: "failed" }
      )
    ).toBe(false);
  });

  it("rejects when the request drops a filter the cursor was bound to", () => {
    expect(
      cursorMatchesFilters({ created_at: "x", id: 1, status: "complete", phase: null }, {})
    ).toBe(false);
  });
});
