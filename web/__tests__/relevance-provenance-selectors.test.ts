import { describe, expect, it } from "vitest";
import {
  selectHoldProvenance,
  selectRelevanceActionBadge,
  selectReleaseOrigin,
} from "@/lib/review-selectors";
import {
  isActionableForPosting,
  isHeld,
  selectSurfaceStatusCounts,
} from "@/lib/transforms";
import type {
  HoldoutRow,
  RelevanceDecisionRow,
  RelevanceProvenance,
  ReleasedFromRow,
} from "@/lib/transforms";
import type { SurfaceStatus } from "@/types/schema";

function decision(overrides: Partial<RelevanceDecisionRow> = {}): RelevanceDecisionRow {
  return {
    decision_uid: "uid-1",
    classifier: "jev",
    model: "jev-latest",
    catalogue_id: "agent-ops-relevance",
    catalogue_version: "ab12cd34ef567890",
    router_version: "agent_ops_route/v1",
    action: "respond",
    reason: "answerable from the post",
    selected_for_holdout: false,
    created_at: "2026-09-20T00:00:00Z",
    ...overrides,
  };
}

function holdout(overrides: Partial<HoldoutRow> = {}): HoldoutRow {
  return {
    id: 1,
    status: "pending",
    held_at: "2026-09-20T00:00:00Z",
    released_at: null,
    release_authority: null,
    release_action: null,
    label: null,
    label_source: null,
    target_evaluation_id: null,
    attempts: 0,
    last_error: null,
    ...overrides,
  };
}

function provenance(overrides: Partial<RelevanceProvenance> = {}): RelevanceProvenance {
  return {
    evaluation_id: 1,
    decision: null,
    holdout: null,
    released_from: null,
    ...overrides,
  };
}

describe("selectRelevanceActionBadge", () => {
  it("labels the classifier and the action it recorded", () => {
    expect(selectRelevanceActionBadge(provenance({ decision: decision() }))).toMatchObject({
      label: "jev · respond",
      tone: "positive",
    });
  });

  it("says a review is classifier metadata, not a promise of surfacing", () => {
    const badge = selectRelevanceActionBadge(
      provenance({ decision: decision({ action: "review" }) })
    );
    expect(badge?.label).toBe("jev · review");
    expect(badge?.title).toContain("not a promise that anything was surfaced");
  });

  it("renders nothing when no classifier was recorded", () => {
    expect(selectRelevanceActionBadge(provenance())).toBeNull();
  });

  it("renders nothing for a database that could not record one", () => {
    expect(selectRelevanceActionBadge(null)).toBeNull();
  });

  it("names the catalogue a JEV decision was made against", () => {
    expect(selectRelevanceActionBadge(provenance({ decision: decision() }))?.title).toContain(
      "agent-ops-relevance @ ab12cd34ef56"
    );
  });

  it("omits catalogue identity from an LLM decision that has none", () => {
    const badge = selectRelevanceActionBadge(
      provenance({
        decision: decision({
          classifier: "llm",
          model: "test-model",
          catalogue_id: null,
          catalogue_version: null,
          router_version: null,
          action: "drop",
        }),
      })
    );
    expect(badge?.label).toBe("llm · drop");
    expect(badge?.title).not.toContain("catalogue");
  });
});

describe("selectHoldProvenance", () => {
  it("reads an unheld evaluation as not held", () => {
    expect(selectHoldProvenance(provenance())).toEqual({ kind: "not_held" });
  });

  it("reads a pending hold as awaiting release", () => {
    expect(selectHoldProvenance(provenance({ holdout: holdout() }))).toMatchObject({
      kind: "awaiting_release",
      status: "pending",
    });
  });

  it("reads a claimed hold as awaiting release, not as released", () => {
    expect(
      selectHoldProvenance(provenance({ holdout: holdout({ status: "claimed" }) }))
    ).toMatchObject({ kind: "awaiting_release", status: "claimed" });
  });

  it("reads a failed attempt with the error that stopped it", () => {
    expect(
      selectHoldProvenance(
        provenance({
          holdout: holdout({ status: "failed", attempts: 2, last_error: "project deleted" }),
        })
      )
    ).toMatchObject({ kind: "release_failed", attempts: 2, last_error: "project deleted" });
  });

  it("reads a label-released hold as released on its label", () => {
    expect(
      selectHoldProvenance(
        provenance({
          holdout: holdout({
            status: "released",
            release_authority: "label",
            release_action: "review",
            label: "pointer",
            target_evaluation_id: 15,
          }),
        })
      )
    ).toMatchObject({ kind: "released", authority: "label", action: "review", label: "pointer" });
  });

  it("reads an ungraded release as acting on the recorded action", () => {
    expect(
      selectHoldProvenance(
        provenance({
          holdout: holdout({
            status: "released",
            release_authority: "recorded_action",
            release_action: "drop",
          }),
        })
      )
    ).toMatchObject({ kind: "released", authority: "recorded_action", action: "drop" });
  });

  it("refuses to read a released row with no authority as a completed release", () => {
    expect(
      selectHoldProvenance(provenance({ holdout: holdout({ status: "released" }) }))
    ).toMatchObject({ kind: "release_failed" });
  });
});

describe("selectReleaseOrigin", () => {
  const origin: ReleasedFromRow = {
    holdout_id: 3,
    source_evaluation_id: 14,
    release_authority: "label",
    release_action: "review",
    label: "pointer",
    label_source: "synthetic-sitting#case-3",
    released_at: "2026-09-23T00:00:00Z",
  };

  it("is absent for an evaluation no release produced", () => {
    expect(selectReleaseOrigin(provenance())).toBeNull();
  });

  it("names the hold and the label that released it", () => {
    const view = selectReleaseOrigin(provenance({ released_from: origin }));
    expect(view?.label).toBe("released · review");
    expect(view?.title).toContain("holdout 3");
    expect(view?.title).toContain("blind label pointer");
  });

  it("names the recorded action when no label released it", () => {
    const view = selectReleaseOrigin(
      provenance({
        released_from: {
          ...origin,
          release_authority: "recorded_action",
          label: null,
          label_source: null,
        },
      })
    );
    expect(view?.title).toContain("the recorded classifier action");
  });

  it("says the authority is not the outcome", () => {
    expect(selectReleaseOrigin(provenance({ released_from: origin }))?.title).toContain(
      "The release authority, not the outcome."
    );
  });
});

describe("surface-status selectors", () => {
  const population: Array<{ surface_status: SurfaceStatus }> = [
    { surface_status: "surfaced" },
    { surface_status: "surfaced" },
    { surface_status: "held" },
    { surface_status: "held" },
    { surface_status: "held" },
    { surface_status: "drafting_failed" },
    { surface_status: "critic_rejected" },
    { surface_status: "not_relevant" },
  ];

  it("counts held as its own status", () => {
    expect(selectSurfaceStatusCounts(population).held).toBe(3);
  });

  it("never adds held to surfaced", () => {
    expect(selectSurfaceStatusCounts(population).surfaced).toBe(2);
  });

  it("never adds held to drafting_failed", () => {
    expect(selectSurfaceStatusCounts(population).drafting_failed).toBe(1);
  });

  it("counts only surfaced rows as actionable for posting", () => {
    expect(selectSurfaceStatusCounts(population).actionable).toBe(2);
  });

  it("totals every row it was given", () => {
    expect(selectSurfaceStatusCounts(population).total).toBe(population.length);
  });

  it("names an unrecorded status rather than dropping the row", () => {
    expect(selectSurfaceStatusCounts([{ surface_status: null }]).by_status).toEqual({
      unrecorded: 1,
    });
  });

  it("reads held as held", () => {
    expect(isHeld("held")).toBe(true);
  });

  it("treats no held row as actionable for posting", () => {
    expect(isActionableForPosting("held")).toBe(false);
  });

  it("treats a drafting failure as not actionable either", () => {
    expect(isActionableForPosting("drafting_failed")).toBe(false);
  });
});
