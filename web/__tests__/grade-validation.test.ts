import { describe, it, expect } from "vitest";
import { validateGradeEnvelope } from "@/lib/grade-validation";

describe("validateGradeEnvelope", () => {
  // --- Body shape ---

  it("rejects a non-object body", () => {
    expect(validateGradeEnvelope("not an object")).not.toEqual([]);
  });

  it("rejects a null body", () => {
    expect(validateGradeEnvelope(null)).not.toEqual([]);
  });

  it("rejects an array body", () => {
    expect(validateGradeEnvelope([])).not.toEqual([]);
  });

  it("rejects an empty body", () => {
    expect(validateGradeEnvelope({})).not.toEqual([]);
  });

  it("rejects unknown grade fields (old v1 fields included)", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "accept",
        comment_quality: 3,
      })
    ).not.toEqual([]);
  });

  // --- Pass path ---

  it("accepts correct + accept (shallow pass)", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "accept",
      })
    ).toEqual([]);
  });

  it("rejects false_positive + accept", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "false_positive",
        action_judgment: "accept",
      })
    ).not.toEqual([]);
  });

  it("rejects false_negative + accept", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "false_negative",
        action_judgment: "accept",
      })
    ).not.toEqual([]);
  });

  it("rejects accept grade with dimensions set", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "accept",
        dimensions: ["tone"],
      })
    ).not.toEqual([]);
  });

  it("rejects accept grade with failure_note", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "accept",
        failure_note: "some note",
      })
    ).not.toEqual([]);
  });

  it("rejects accept grade with stale causal field", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "accept",
        factual_offending_claim: "some claim",
      })
    ).not.toEqual([]);
  });

  it("accepts an explicit-null causal fields accept grade", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "accept",
        dimensions: null,
        failure_note: null,
        factual_offending_claim: null,
        factual_disposition: null,
        factual_contradicting_evidence: null,
        context_missing_input: null,
        posture_should_have_been: null,
        implication_implied_claim: null,
        implication_missing_support: null,
      })
    ).toEqual([]);
  });

  // --- Fail path — base requirements ---

  it("rejects fail without dimensions", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        failure_note: "bad",
      })
    ).not.toEqual([]);
  });

  it("rejects fail without failure_note", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["tone"],
      })
    ).not.toEqual([]);
  });

  it("rejects fail with empty dimensions", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: [],
        failure_note: "bad",
      })
    ).not.toEqual([]);
  });

  it("rejects fail with unknown dimension", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["bad_dim"],
        failure_note: "note",
      })
    ).not.toEqual([]);
  });

  it("rejects fail with duplicate dimensions", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["tone", "tone"],
        failure_note: "note",
      })
    ).not.toEqual([]);
  });

  it("rejects fail with whitespace-only failure_note", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["tone"],
        failure_note: "   ",
      })
    ).not.toEqual([]);
  });

  it("accepts tone fail (no extra causal fields needed)", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["tone"],
        failure_note: "overly formal",
      })
    ).toEqual([]);
  });

  it("accepts usefulness fail", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["usefulness"],
        failure_note: "no actionable step",
      })
    ).toEqual([]);
  });

  // --- factual_support dimension ---

  it("rejects factual_support without offending_claim and disposition", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["factual_support"],
        failure_note: "wrong fact",
      })
    ).not.toEqual([]);
  });

  it("accepts factual_support unsupported", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["factual_support"],
        failure_note: "no source",
        factual_offending_claim: "50k users",
        factual_disposition: "unsupported",
      })
    ).toEqual([]);
  });

  it("rejects factual_support unsupported with evidence", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["factual_support"],
        failure_note: "note",
        factual_offending_claim: "claim",
        factual_disposition: "unsupported",
        factual_contradicting_evidence: "some evidence",
      })
    ).not.toEqual([]);
  });

  it("rejects factual_support contradicted without evidence (T-004)", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["factual_support"],
        failure_note: "note",
        factual_offending_claim: "claim",
        factual_disposition: "contradicted",
      })
    ).not.toEqual([]);
  });

  it("accepts factual_support contradicted with evidence", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["factual_support"],
        failure_note: "contradicted",
        factual_offending_claim: "claim",
        factual_disposition: "contradicted",
        factual_contradicting_evidence: "docs say otherwise",
      })
    ).toEqual([]);
  });

  it("rejects factual causal fields when dimension absent", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["tone"],
        failure_note: "note",
        factual_offending_claim: "stale claim",
      })
    ).not.toEqual([]);
  });

  // --- contextual_understanding dimension ---

  it("rejects contextual_understanding without missing_input", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["contextual_understanding"],
        failure_note: "missed context",
      })
    ).not.toEqual([]);
  });

  it("accepts contextual_understanding with missing_input", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["contextual_understanding"],
        failure_note: "missed context",
        context_missing_input: "parent post was a ZK announcement",
      })
    ).toEqual([]);
  });

  it("rejects context_missing_input when dimension absent", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["tone"],
        failure_note: "note",
        context_missing_input: "stale",
      })
    ).not.toEqual([]);
  });

  // --- posture dimension ---

  it("rejects posture without should_have_been", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["posture"],
        failure_note: "wrong posture",
      })
    ).not.toEqual([]);
  });

  it("accepts posture with valid should_have_been", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["posture"],
        failure_note: "should have asked",
        posture_should_have_been: "ask",
      })
    ).toEqual([]);
  });

  it("rejects posture_should_have_been when dimension absent", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["tone"],
        failure_note: "note",
        posture_should_have_been: "ask",
      })
    ).not.toEqual([]);
  });

  it("rejects a posture correction equal to the evaluation's actual posture", () => {
    const input = {
      relevance_judgment: "correct",
      action_judgment: "fail",
      dimensions: ["posture"],
      failure_note: "wrong posture",
      posture_should_have_been: "ask",
    };
    expect(validateGradeEnvelope(input, "ask")).not.toEqual([]);
  });

  it("accepts a posture correction that differs from the evaluation's actual posture", () => {
    const input = {
      relevance_judgment: "correct",
      action_judgment: "fail",
      dimensions: ["posture"],
      failure_note: "wrong posture",
      posture_should_have_been: "ask",
    };
    expect(validateGradeEnvelope(input, "answer")).toEqual([]);
  });

  it("allows any valid posture correction when the evaluation posture is unknown (null)", () => {
    const input = {
      relevance_judgment: "correct",
      action_judgment: "fail",
      dimensions: ["posture"],
      failure_note: "wrong posture",
      posture_should_have_been: "ask",
    };
    expect(validateGradeEnvelope(input, null)).toEqual([]);
  });

  it("covers all four equal-posture branches", () => {
    for (const posture of ["answer", "engage", "ask", "abstain"]) {
      const input = {
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["posture"],
        failure_note: "wrong posture",
        posture_should_have_been: posture,
      };
      expect(validateGradeEnvelope(input, posture)).not.toEqual([]);
    }
  });

  // --- unsupported_implication dimension ---

  it("rejects unsupported_implication without implied_claim and missing_support", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["unsupported_implication"],
        failure_note: "implied without basis",
      })
    ).not.toEqual([]);
  });

  it("accepts unsupported_implication with both fields", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["unsupported_implication"],
        failure_note: "implied community",
        implication_implied_claim: "active community",
        implication_missing_support: "no community fact in dossier",
      })
    ).toEqual([]);
  });

  it("rejects implication fields when dimension absent", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["tone"],
        failure_note: "note",
        implication_implied_claim: "stale claim",
      })
    ).not.toEqual([]);
  });

  // --- Multi-dimension ---

  it("accepts multi-dimension fail", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["contextual_understanding", "posture"],
        failure_note: "misread context and wrong posture",
        context_missing_input: "parent post about ZK proofs",
        posture_should_have_been: "ask",
      })
    ).toEqual([]);
  });

  it("accepts false_positive fail", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "false_positive",
        action_judgment: "fail",
        dimensions: ["contextual_understanding"],
        failure_note: "scout should not have flagged this",
        context_missing_input: "post was asking about a competitor",
      })
    ).toEqual([]);
  });

  // --- Invalid vocabulary ---

  it("rejects invalid relevance_judgment", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "wrong",
        action_judgment: "accept",
      })
    ).not.toEqual([]);
  });

  it("rejects invalid action_judgment", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "wrong",
      })
    ).not.toEqual([]);
  });

  it("requires relevance_judgment", () => {
    expect(validateGradeEnvelope({ action_judgment: "accept" })).not.toEqual([]);
  });

  it("requires action_judgment", () => {
    expect(validateGradeEnvelope({ relevance_judgment: "correct" })).not.toEqual([]);
  });

  // --- edited_text (v3) ---

  it("rejects an accept grade with edited_text", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "accept",
        edited_text: "a corrected reply",
      })
    ).not.toEqual([]);
  });

  it("accepts an accept grade with edited_text explicitly null", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "accept",
        edited_text: null,
      })
    ).toEqual([]);
  });

  it("rejects fail with only a whitespace-only edited_text", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["tone"],
        edited_text: "   ",
      })
    ).not.toEqual([]);
  });

  it("rejects fail with edited_text but no dimensions", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        edited_text: "a corrected reply",
      })
    ).not.toEqual([]);
  });

  it("accepts fail with edited_text alone, no failure_note", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["tone"],
        edited_text: "a less formal rewrite of the draft reply",
      })
    ).toEqual([]);
  });

  it("accepts fail with valid causal detail alone, no failure_note or edited_text", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["factual_support"],
        factual_offending_claim: "We have 50k users",
        factual_disposition: "unsupported",
      })
    ).toEqual([]);
  });

  it("still rejects a tone-only fail with none of failure_note, edited_text, or causal detail", () => {
    expect(
      validateGradeEnvelope({
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["tone"],
      })
    ).not.toEqual([]);
  });
});
