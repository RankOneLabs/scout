import { describe, it, expect } from "vitest";
import {
  postFiltersSchema,
  draftFiltersSchema,
  evaluationFiltersSchema,
  negativeGradingFiltersSchema,
  parseSearchParams,
} from "@/lib/filter-schemas";

describe("postFiltersSchema", () => {
  it("accepts valid minimal input", () => {
    const result = postFiltersSchema.safeParse({});
    expect(result.success).toBe(true);
  });

  it("accepts valid platform and score range", () => {
    const result = postFiltersSchema.safeParse({
      platform: "discord",
      score_min: "0.2",
      score_max: "0.9",
      scan_id: "5",
      before_id: "100",
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.platform).toBe("discord");
      expect(result.data.score_min).toBe(0.2);
      expect(result.data.score_max).toBe(0.9);
      expect(result.data.scan_id).toBe(5);
      expect(result.data.before_id).toBe(100);
    }
  });

  it("coerces relevant=true to boolean true", () => {
    const result = postFiltersSchema.safeParse({ relevant: "true" });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.relevant).toBe(true);
  });

  it("coerces relevant=false to boolean false", () => {
    const result = postFiltersSchema.safeParse({ relevant: "false" });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.relevant).toBe(false);
  });

  it("rejects score_min=abc (NaN)", () => {
    const result = postFiltersSchema.safeParse({ score_min: "abc" });
    expect(result.success).toBe(false);
  });

  it("rejects platform=invalid", () => {
    const result = postFiltersSchema.safeParse({ platform: "invalid" });
    expect(result.success).toBe(false);
  });

  it("rejects score_min=1.5 (out of range)", () => {
    const result = postFiltersSchema.safeParse({ score_min: "1.5" });
    expect(result.success).toBe(false);
  });

  it("rejects score_min=-0.1 (out of range)", () => {
    const result = postFiltersSchema.safeParse({ score_min: "-0.1" });
    expect(result.success).toBe(false);
  });

  it("rejects scan_id=0 (must be positive)", () => {
    const result = postFiltersSchema.safeParse({ scan_id: "0" });
    expect(result.success).toBe(false);
  });

  it("rejects before_id=abc (NaN)", () => {
    const result = postFiltersSchema.safeParse({ before_id: "abc" });
    expect(result.success).toBe(false);
  });

  it("rejects relevant=yes", () => {
    const result = postFiltersSchema.safeParse({ relevant: "yes" });
    expect(result.success).toBe(false);
  });
});

describe("draftFiltersSchema", () => {
  it("accepts valid minimal input", () => {
    const result = draftFiltersSchema.safeParse({});
    expect(result.success).toBe(true);
  });

  it("accepts valid verdict and project_key", () => {
    const result = draftFiltersSchema.safeParse({
      verdict: "approve",
      project_key: "my-project",
      scan_id: "3",
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.verdict).toBe("approve");
      expect(result.data.project_key).toBe("my-project");
      expect(result.data.scan_id).toBe(3);
    }
  });

  it("rejects verdict=unknown", () => {
    const result = draftFiltersSchema.safeParse({ verdict: "unknown" });
    expect(result.success).toBe(false);
  });

  it("rejects project_key=''", () => {
    const result = draftFiltersSchema.safeParse({ project_key: "" });
    expect(result.success).toBe(false);
  });

  it("coerces include_grades=true to boolean", () => {
    const result = draftFiltersSchema.safeParse({ include_grades: "true" });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.include_grades).toBe(true);
  });

  it("coerces missing include_grades to false", () => {
    const result = draftFiltersSchema.safeParse({});
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.include_grades).toBe(false);
  });
});

describe("evaluationFiltersSchema", () => {
  it("accepts valid scan_id", () => {
    const result = evaluationFiltersSchema.safeParse({ scan_id: "7" });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.scan_id).toBe(7);
  });

  it("rejects missing scan_id", () => {
    const result = evaluationFiltersSchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it("rejects scan_id=abc", () => {
    const result = evaluationFiltersSchema.safeParse({ scan_id: "abc" });
    expect(result.success).toBe(false);
  });
});

describe("negativeGradingFiltersSchema", () => {
  it("accepts and coerces pagination parameters", () => {
    const result = negativeGradingFiltersSchema.safeParse({ before_id: "40", limit: "25" });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data).toEqual({ before_id: 40, limit: 25 });
    }
  });

  it("rejects non-positive cursors and oversized pages", () => {
    expect(negativeGradingFiltersSchema.safeParse({ before_id: "0" }).success).toBe(false);
    expect(negativeGradingFiltersSchema.safeParse({ limit: "201" }).success).toBe(false);
  });
});

describe("parseSearchParams", () => {
  it("returns ok:true with typed data on good input", () => {
    const params = new URLSearchParams({
      platform: "discord",
      score_min: "0.5",
    });
    const result = parseSearchParams(params, postFiltersSchema);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.platform).toBe("discord");
      expect(result.data.score_min).toBe(0.5);
    }
  });

  it("returns ok:false with error list on bad input", () => {
    const params = new URLSearchParams({
      platform: "invalid",
      score_min: "abc",
    });
    const result = parseSearchParams(params, postFiltersSchema);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.errors.length).toBeGreaterThan(0);
      expect(result.errors.some((e) => e.includes("platform"))).toBe(true);
      expect(result.errors.some((e) => e.includes("score_min"))).toBe(true);
    }
  });

  it("returns ok:true with empty URLSearchParams", () => {
    const params = new URLSearchParams();
    const result = parseSearchParams(params, postFiltersSchema);
    expect(result.ok).toBe(true);
  });

  it("treats empty query values as absent (does not coerce to 0)", () => {
    // `?score_min=&scan_id=` would otherwise become score_min=0, scan_id=0.
    const params = new URLSearchParams();
    params.append("score_min", "");
    params.append("score_max", "");
    params.append("scan_id", "");
    params.append("platform", "");
    const result = parseSearchParams(params, postFiltersSchema);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.score_min).toBeUndefined();
      expect(result.data.score_max).toBeUndefined();
      expect(result.data.scan_id).toBeUndefined();
      expect(result.data.platform).toBeUndefined();
    }
  });
});
