"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ExperimentRunListResponse, ExperimentRunSummary } from "@/types/feedback-experiments";

export interface ExperimentListQueryFilters {
  status?: string;
  phase?: string;
}

function toSearchParams(filters: ExperimentListQueryFilters, cursor?: string): URLSearchParams {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value === undefined || value === "") continue;
    search.set(key, value);
  }
  if (cursor) search.set("cursor", cursor);
  return search;
}

async function fetchPage(
  filters: ExperimentListQueryFilters,
  cursor?: string
): Promise<ExperimentRunListResponse> {
  const res = await fetch(`/api/feedback/experiment-runs?${toSearchParams(filters, cursor).toString()}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<ExperimentRunListResponse>;
}

// Server-authoritative filtering/ordering/paging — this hook only fetches
// pages and appends them, mirroring useGradeList's contract.
export function useExperimentList(filters: ExperimentListQueryFilters) {
  const [data, setData] = useState<ExperimentRunSummary[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
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
      })
      .catch((err: unknown) => {
        if (id !== fetchId.current) return;
        setError(err instanceof Error ? err.message : String(err));
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtersKey, hasMore, nextCursor]);

  return { data, hasMore, loading, error, loadMore };
}
