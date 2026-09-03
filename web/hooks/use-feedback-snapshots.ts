"use client";

import { useEffect, useState } from "react";
import type { SnapshotListItem } from "@/types/feedback";

export interface UseFeedbackSnapshotsParams {
  scanId?: number;
  mode?: "shadow" | "active";
  limit?: number;
}

export function useFeedbackSnapshots(params: UseFeedbackSnapshotsParams) {
  const [data, setData] = useState<SnapshotListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { scanId, mode, limit } = params;

  useEffect(() => {
    const search = new URLSearchParams();
    if (scanId !== undefined) search.set("scanId", String(scanId));
    if (mode !== undefined) search.set("mode", mode);
    if (limit !== undefined) search.set("limit", String(limit));

    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    fetch(`/api/feedback/snapshots?${search.toString()}`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<{ data: SnapshotListItem[] }>;
      })
      .then((page) => {
        if (cancelled) return;
        setData(page.data);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setData([]);
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [scanId, mode, limit]);

  return { data, loading, error };
}
