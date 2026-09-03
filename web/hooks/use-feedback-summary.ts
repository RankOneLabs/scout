"use client";

import { useEffect, useRef, useState } from "react";
import type { FeedbackSummaryResponse } from "@/types/feedback-summary";

export interface UseFeedbackSummaryFilters {
  from?: string;
  to?: string;
}

async function fetchSummary(filters: UseFeedbackSummaryFilters): Promise<FeedbackSummaryResponse> {
  const search = new URLSearchParams();
  if (filters.from) search.set("from", filters.from);
  if (filters.to) search.set("to", filters.to);
  const qs = search.toString();
  const res = await fetch(`/api/feedback/summary${qs ? `?${qs}` : ""}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<FeedbackSummaryResponse>;
}

// All corpus metrics and filters are server-authoritative — this hook only
// fetches the already-computed summary, never derives totals client-side.
export function useFeedbackSummary(filters: UseFeedbackSummaryFilters) {
  const [data, setData] = useState<FeedbackSummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fetchId = useRef(0);

  useEffect(() => {
    const id = ++fetchId.current;
    setLoading(true);
    setError(null);
    fetchSummary(filters)
      .then((resp) => {
        if (id !== fetchId.current) return;
        setData(resp);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (id !== fetchId.current) return;
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters.from, filters.to]);

  return { data, loading, error };
}
