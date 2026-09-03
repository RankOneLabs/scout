import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";
import Ajv2020 from "ajv/dist/2020";

import schema from "../grading_schema.json";

// Loads the same language-neutral fixture matrix tests/test_grading_contract.py
// loads on the Python side (tests/fixtures/grading_contract_cases.json) and
// asserts Ajv2020 produces the identical valid/invalid verdict for every
// entry as jsonschema.Draft202012Validator does — this is the cross-runtime
// proof that Python, TypeScript, and (by construction, via the shared
// canonical $id) the eval corpus schema agree on the full conditional
// surface, including contextual posture inequality. Diagnostic wording is
// not compared (see decision 14) — only the boolean verdict.

interface FixtureCase {
  name: string;
  tags: string[];
  valid: boolean;
  envelope: unknown;
}

const fixturePath = path.resolve(
  __dirname,
  "../../tests/fixtures/grading_contract_cases.json"
);
const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8")) as {
  cases: FixtureCase[];
};

describe("grading contract schema compiles", () => {
  it("compiles under Ajv2020 in strict + allErrors mode", () => {
    const ajv = new Ajv2020({ allErrors: true, strict: true });
    expect(() => ajv.compile(schema)).not.toThrow();
  });

  it("declares a stable canonical $id", () => {
    expect((schema as { $id?: string }).$id).toBeTruthy();
  });

  it("defines all required $defs", () => {
    const defs = (schema as { $defs?: Record<string, unknown> }).$defs ?? {};
    for (const name of [
      "relevanceJudgment",
      "actionJudgment",
      "failureDimension",
      "posture",
      "factualDisposition",
      "nonBlankString",
      "gradePayload",
      "validationEnvelope",
    ]) {
      expect(defs).toHaveProperty(name);
    }
  });
});

describe("grading contract fixture matrix", () => {
  const ajv = new Ajv2020({ allErrors: true, strict: true });
  const validate = ajv.compile(schema);

  it("loaded a non-empty fixture matrix", () => {
    expect(fixture.cases.length).toBeGreaterThan(0);
  });

  it.each(fixture.cases.map((c) => [c.name, c]))(
    "%s",
    (_name, testCase) => {
      const c = testCase as FixtureCase;
      const actualValid = validate(c.envelope);
      expect(actualValid).toBe(c.valid);
    }
  );
});

describe("grading contract fixture coverage", () => {
  it("has unique case names", () => {
    const names = fixture.cases.map((c) => c.name);
    expect(new Set(names).size).toBe(names.length);
  });

  it("covers both valid and invalid verdicts", () => {
    expect(fixture.cases.some((c) => c.valid)).toBe(true);
    expect(fixture.cases.some((c) => !c.valid)).toBe(true);
  });

  it("covers all four equal-posture branches", () => {
    const names = new Set(fixture.cases.map((c) => c.name));
    for (const posture of ["answer", "engage", "ask", "abstain"]) {
      expect(names.has(`invalid_equal_posture_${posture}`)).toBe(true);
    }
  });

  it("covers the contradicted-without-evidence case (T-004)", () => {
    const names = new Set(fixture.cases.map((c) => c.name));
    expect(
      names.has("invalid_factual_support_contradicted_without_evidence_T004")
    ).toBe(true);
  });

  it("covers an unknown-field rejection", () => {
    const tags = new Set(fixture.cases.flatMap((c) => c.tags));
    expect(tags.has("unknown-field")).toBe(true);
  });
});
