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
  it("separates the exact blocking set from non-blocking degradation", () => {
    const blocking = makeFailure({ id: 1, blocked_watermark: true });
    const nonBlocking = makeFailure({
      id: 2,
      operation_phase: "parent_lookup",
      blocks_watermark_advance: false,
      blocked_watermark: false,
    });
    const { blocking: blockingOut, nonBlocking: nonBlockingOut } = partitionFailuresByBlocking([
      blocking,
      nonBlocking,
    ]);
    expect(blockingOut.map((f) => f.id)).toEqual([1]);
    expect(nonBlockingOut.map((f) => f.id)).toEqual([2]);
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

  it("marks a canonical_live scan's watermark stale when past the threshold", () => {
    const scan = makeScanDetail({
      role: "canonical_live",
      safe_watermark_at: "2000-01-01T00:00:00Z",
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
    expect(screen.getByText("stale")).toBeTruthy();
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
});
