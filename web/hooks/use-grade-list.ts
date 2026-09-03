"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { GradeExplorerRow } from "@/types/feedback-grades";

export interface GradeListQueryFilters {
  gradedFrom?: string;
  gradedTo?: string;
  editedFrom?: string;
  editedTo?: string;
  schemaVersion?: 1 | 2;
  needsRegrade?: boolean;
  project?: string;
  platform?: string;
  posture?: string;
  terminalStatus?: string;
  relevanceJudgment?: string;
  actionJudgment?: string;
  failureDimension?: string;
  eligibilityReason?: string;
  overrideMode?: string;
  snapshotUse?: string;
  traceAvailability?: string;
  scanId?: number;
  evaluationId?: number;
}

interface GradeListPage {
  data: GradeExplorerRow[];
  has_more: boolean;
  next_cursor: string | null;
  total_matching: number;
}

function toSearchParams(filters: GradeListQueryFilters, cursor?: string): URLSearchParams {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  if (cursor) search.set("cursor", cursor);
  search.set("limit", "50");
  return search;
}

async function fetchPage(filters: GradeListQueryFilters, cursor?: string): Promise<GradeListPage> {
  const res = await fetch(`/api/feedback/grades?${toSearchParams(filters, cursor).toString()}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<GradeListPage>;
}

// Filtering, ordering, and total_matching are all server-authoritative —
// this hook fetches pages and appends them, never re-derives totals from
// the rows it happens to hold client-side.
export function useGradeList(filters: GradeListQueryFilters) {
  const [data, setData] = useState<GradeExplorerRow[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [totalMatching, setTotalMatching] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fetchId = useRef(0);
  const filtersKey = JSON.stringify(filters);

  useEffect(() => {
    const id = ++fetchId.current;
    setLoading(true);
    setError(null);
    fetchPage(filters)
      .then((page) => {
        if (id !== fetchId.current) return;
        setData(page.data);
        setHasMore(page.has_more);
        setNextCursor(page.next_cursor);
        setTotalMatching(page.total_matching);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (id !== fetchId.current) return;
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtersKey]);

  const loadMore = useCallback(() => {
    if (!hasMore || !nextCursor) return;
    const id = ++fetchId.current;
    fetchPage(filters, nextCursor)
      .then((page) => {
        if (id !== fetchId.current) return;
        setData((current) => [...current, ...page.data]);
        setHasMore(page.has_more);
        setNextCursor(page.next_cursor);
        setTotalMatching(page.total_matching);
      })
      .catch((err: unknown) => {
        if (id !== fetchId.current) return;
        setError(err instanceof Error ? err.message : String(err));
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtersKey, hasMore, nextCursor]);

  return { data, hasMore, totalMatching, loading, error, loadMore };
}
