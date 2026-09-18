// @vitest-environment jsdom

import React from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ScanTable } from "@/components/organisms/ScanTable";
import { ScanDetailView } from "@/components/organisms/ScanDetailView";
import {
  describeCoverage,
  isNonBlockingDegradation,
  partitionFailuresByBlocking,
  isWatermarkStale,
  isLeaseHeld,
} from "@/lib/transforms";
import type { Scan, ScanDetailWithCounts, ScanFetchFailure } from "@/types/schema";

afterEach(() => {
  cleanup();
});

function makeScan(overrides: Partial<Scan> = {}): Scan {
  return {
    id: 1,
    started_at: "2026-05-15T00:00:00+00:00",
    completed_at: "2026-05-15T00:05:00+00:00",
    messages_scanned: 3,
    relevant_found: 1,
    fetch_started_at: "2026-05-15T00:00:00+00:00",
    safe_watermark_at: "2026-05-15T00:00:00+00:00",
    status: "complete",
    overflow_count: 0,
    environment: "production",
    run_kind: "live",
    role: "canonical_live",
    coverage_outcome: "complete",
    watermark_advanced: true,
    coverage_classifier_version: 1,
    canonical_scan_id: null,
    ...overrides,
  };
}

function makeFailure(overrides: Partial<ScanFetchFailure> = {}): ScanFetchFailure {
  return {
    id: 1,
    scan_id: 1,
    platform: "bluesky",
    context: "src-a",
    kind: "page_ceiling",
    message: "hit page ceiling",
    http_status: null,
    retry_after: null,
    retryable: true,
    created_at: "2026-05-15T00:00:00+00:00",
    operation_phase: "fetch",
    blocks_watermark_advance: true,
    blocked_watermark: true,
    ...overrides,
  };
}

function makeScanDetail(overrides: Partial<ScanDetailWithCounts> = {}): ScanDetailWithCounts {
  return {
    ...makeScan(),
    post_count: 0,
    eval_count: 0,
    draft_count: 0,
    approved_count: 0,
    rejected_count: 0,
    revised_count: 0,
    critique_count: 0,
    failures: [],
    source_checkpoints: [],
    environment_lease: null,
    environment_watermark_at: "2026-05-15T00:00:00+00:00",
    recent_recovery_operations: [],
    latest_probe_run: null,
    ...overrides,
  };
}

// --- describeCoverage / staleness / partition selectors ---

describe("describeCoverage", () => {
  it("reports a non-blocking degraded scan as watermark advanced, independent of status", () => {
    const scan = makeScan({ status: "partial", coverage_outcome: "complete", watermark_advanced: true });
    expect(describeCoverage(scan)).toMatchObject({ tone: "ok" });
    expect(isNonBlockingDegradation(scan)).toBe(true);
  });

  it("reports a fully-processed but coverage-blocked scan distinctly", () => {
    const scan = makeScan({ status: "complete", coverage_outcome: "blocked", watermark_advanced: false });
    expect(describeCoverage(scan)).toMatchObject({ tone: "danger", label: "coverage blocked" });
  });

  it("never claims advancement for a secondary or rescore scan", () => {
    expect(describeCoverage(makeScan({ role: "secondary", coverage_outcome: "complete", watermark_advanced: false })).tone).toBe(
      "neutral"
    );
    expect(describeCoverage(makeScan({ role: "rescore", coverage_outcome: "complete", watermark_advanced: false })).tone).toBe(
      "neutral"
    );
  });
});

describe("partitionFailuresByBlocking", () => {
  it("separates counted blockers, unadjudicated blocking-eligible failures, and non-blocking degradation", () => {
    const blocking = makeFailure({ id: 1, blocked_watermark: true });
    // Blocking-eligible but never counted: the scan's finalization never
    // ran (failed/interrupted), so no scan_watermark_blockers row exists.
    const eligibleUncounted = makeFailure({
      id: 3,
      kind: "abandoned_owner",
      blocks_watermark_advance: true,
      blocked_watermark: false,
    });
    const nonBlocking = makeFailure({
      id: 2,
      operation_phase: "parent_lookup",
      blocks_watermark_advance: false,
      blocked_watermark: false,
    });
    const out = partitionFailuresByBlocking([blocking, eligibleUncounted, nonBlocking]);
    expect(out.blocking.map((f) => f.id)).toEqual([1]);
    expect(out.eligibleUncounted.map((f) => f.id)).toEqual([3]);
    expect(out.nonBlocking.map((f) => f.id)).toEqual([2]);
  });
});

describe("isLeaseHeld", () => {
  it("treats a lingering owner_id on an expired lease as not held", () => {
    const lease = { owner_id: "owner-1", expires_at: "2026-05-15T00:00:00Z" };
    expect(isLeaseHeld(lease, "2026-05-14T23:00:00Z")).toBe(true);
    expect(isLeaseHeld(lease, "2026-05-15T00:00:01Z")).toBe(false);
    expect(isLeaseHeld({ owner_id: null, expires_at: null }, "2026-05-14T23:00:00Z")).toBe(false);
  });
});

describe("isWatermarkStale", () => {
  it("treats a null watermark as always stale", () => {
    expect(isWatermarkStale(null, "2026-05-15T00:00:00Z")).toBe(true);
  });

  it("is fresh within the threshold and stale beyond it", () => {
    const watermark = "2026-05-15T00:00:00Z";
    expect(isWatermarkStale(watermark, "2026-05-15T12:00:00Z", 24)).toBe(false);
    expect(isWatermarkStale(watermark, "2026-05-16T01:00:00Z", 24)).toBe(true);
  });
});

// --- ScanTable rendering ---

describe("ScanTable coverage presentation", () => {
  it("renders status and coverage badges as distinct facts", () => {
    render(
      React.createElement(ScanTable, {
        scans: [makeScan({ id: 5, status: "partial", coverage_outcome: "complete", watermark_advanced: true })],
      })
    );
    expect(screen.getAllByText("partial").length).toBeGreaterThan(0);
    expect(screen.getAllByText("watermark advanced").length).toBeGreaterThan(0);
  });

  it("renders a blocked-coverage scan distinctly from a partial-status one", () => {
    render(
      React.createElement(ScanTable, {
        scans: [makeScan({ id: 6, status: "complete", coverage_outcome: "blocked", watermark_advanced: false })],
      })
    );
    expect(screen.getAllByText("complete").length).toBeGreaterThan(0);
    expect(screen.getAllByText("coverage blocked").length).toBeGreaterThan(0);
  });

  it("links a secondary scan's environment cell to its canonical owner", () => {
    render(
      React.createElement(ScanTable, {
        scans: [makeScan({ id: 7, role: "secondary", canonical_scan_id: 6 })],
      })
    );
    const links = screen.getAllByRole("link", { name: /#6/ });
    expect(links.some((l) => l.getAttribute("href") === "/scans/6")).toBe(true);
  });
});

// --- ScanDetailView rendering ---

describe("ScanDetailView coverage presentation", () => {
  const noop = () => {};

  it("shows a scan's blocking failure by id and operation phase, separate from non-blocking degradation", () => {
    const scan = makeScanDetail({
      id: 6,
      status: "complete",
      coverage_outcome: "blocked",
      watermark_advanced: false,
      failures: [
        makeFailure({ id: 1002, operation_phase: "fetch", blocked_watermark: true }),
        makeFailure({
          id: 1001,
          operation_phase: "parent_lookup",
          blocks_watermark_advance: false,
          blocked_watermark: false,
        }),
      ],
    });
    render(
      React.createElement(ScanDetailView, {
        scan,
        posts: [],
        evaluations: [],
        postFilters: {},
        onPostFilterChange: noop,
      })
    );
    expect(screen.getByText(/Blocking Failures \(1\)/)).toBeTruthy();
    expect(screen.getByText(/Non-blocking Degradation \(1\)/)).toBeTruthy();
    expect(screen.getByText("1002")).toBeTruthy();
    expect(screen.getByText("1001")).toBeTruthy();
  });

  it("renders the environment's current source checkpoints", () => {
    const scan = makeScanDetail({
      source_checkpoints: [
        {
          source_key: "bluesky:search:src-a",
          platform: "bluesky",
          source_kind: "search",
          provider_key: "src-a",
          required: true,
          active: true,
          checkpoint_at: "2026-05-15T00:00:00Z",
          bootstrapped_from_legacy: false,
        },
      ],
    });
    render(
      React.createElement(ScanDetailView, {
        scan,
        posts: [],
        evaluations: [],
        postFilters: {},
        onPostFilterChange: noop,
      })
    );
    expect(screen.getByText("bluesky:search:src-a")).toBeTruthy();
  });

  it("does not render an empty source-checkpoint heading", () => {
    render(
      React.createElement(ScanDetailView, {
        scan: makeScanDetail({ source_checkpoints: [] }),
        posts: [],
        evaluations: [],
        postFilters: {},
        onPostFilterChange: noop,
      })
    );
    expect(screen.queryByText("Source Checkpoints")).toBeNull();
  });

  it("renders a probe without a verdict as in progress", () => {
    render(
      React.createElement(ScanDetailView, {
        scan: makeScanDetail({
          latest_probe_run: {
            id: 4,
            environment: "production",
            started_at: "2026-05-15T00:00:00Z",
            completed_at: null,
            passed: null,
            source_count: 1,
            page_count: 0,
            window_hours: 6,
            limits_json: "{}",
            detail_json: "{}",
            created_at: "2026-05-15T00:00:00Z",
          },
        }),
        posts: [],
        evaluations: [],
        postFilters: {},
        onPostFilterChange: noop,
      })
    );
    expect(screen.getByText(/Latest probe: in progress/)).toBeTruthy();
    expect(screen.queryByText(/Latest probe: failed/)).toBeNull();
  });

  it("judges staleness by the environment's current watermark, not the selected scan's historical one", () => {
    // An old, blocked scan (own watermark null) in an environment a later
    // scan kept fresh must not read as stale.
    const freshEnv = makeScanDetail({
      role: "canonical_live",
      coverage_outcome: "blocked",
      watermark_advanced: false,
      safe_watermark_at: null,
      environment_watermark_at: new Date().toISOString(),
    });
    const { unmount } = render(
      React.createElement(ScanDetailView, {
        scan: freshEnv,
        posts: [],
        evaluations: [],
        postFilters: {},
        onPostFilterChange: noop,
      })
    );
    expect(screen.getByText("fresh")).toBeTruthy();
    unmount();

    // And a scan whose own watermark is recent does not mask a stale
    // environment cursor.
    const staleEnv = makeScanDetail({
      role: "canonical_live",
      safe_watermark_at: new Date().toISOString(),
      environment_watermark_at: "2000-01-01T00:00:00Z",
    });
    render(
      React.createElement(ScanDetailView, {
        scan: staleEnv,
        posts: [],
        evaluations: [],
        postFilters: {},
        onPostFilterChange: noop,
      })
    );
    expect(screen.getByText("stale")).toBeTruthy();
  });

  it("renders an expired lease as expired, not held, even with a lingering owner_id", () => {
    const scan = makeScanDetail({
      environment_lease: {
        environment: "production",
        fence: 3,
        owner_id: "owner-1",
        expires_at: "2000-01-01T00:00:00Z",
        updated_at: "2000-01-01T00:00:00Z",
      },
    });
    render(
      React.createElement(ScanDetailView, {
        scan,
        posts: [],
        evaluations: [],
        postFilters: {},
        onPostFilterChange: noop,
      })
    );
    expect(screen.getByText(/expired \(last held by owner-1\)/)).toBeTruthy();
    expect(screen.queryByText(/^Lease: fence 3, held by/)).toBeNull();
  });

  it("shows an interrupted owner's blocking-eligible failure as unadjudicated, not as non-blocking degradation", () => {
    const scan = makeScanDetail({
      id: 9,
      status: "interrupted",
      coverage_outcome: null,
      watermark_advanced: false,
      safe_watermark_at: null,
      failures: [
        makeFailure({
          id: 1003,
          kind: "abandoned_owner",
          operation_phase: "scan",
          blocks_watermark_advance: true,
          blocked_watermark: false,
        }),
      ],
    });
    render(
      React.createElement(ScanDetailView, {
        scan,
        posts: [],
        evaluations: [],
        postFilters: {},
        onPostFilterChange: noop,
      })
    );
    expect(screen.getByText(/Blocking-eligible Failures \(1\)/)).toBeTruthy();
    expect(screen.queryByText(/Non-blocking Degradation/)).toBeNull();
    expect(screen.queryByText(/^Blocking Failures/)).toBeNull();
  });

  it("does not render the watermark/lease section for a secondary scan", () => {
    const scan = makeScanDetail({ role: "secondary", canonical_scan_id: 1 });
    render(
      React.createElement(ScanDetailView, {
        scan,
        posts: [],
        evaluations: [],
        postFilters: {},
        onPostFilterChange: noop,
      })
    );
    expect(screen.queryByText("Watermark, Lease & Recovery")).toBeNull();
  });

  it("does not render recovery diagnostics for a non-live canonical-role scan", () => {
    const scan = makeScanDetail({ role: "canonical_live", run_kind: "rescore" });
    render(
      React.createElement(ScanDetailView, {
        scan,
        posts: [],
        evaluations: [],
        postFilters: {},
        onPostFilterChange: noop,
      })
    );
    expect(screen.queryByText("Watermark, Lease & Recovery")).toBeNull();
  });
});
