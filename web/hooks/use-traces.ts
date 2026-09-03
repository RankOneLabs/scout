"use client";

import { useState, useEffect } from "react";
import type { TraceListItem, TraceFilters } from "@/types/schema";

export function useTraces(filters?: TraceFilters) {
  const [traces, setTraces] = useState<TraceListItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const params = new URLSearchParams();
    if (filters?.error_only) params.set("error_only", "true");
    if (filters?.limit) params.set("limit", String(filters.limit));

    const url = `/api/traces${params.size ? `?${params}` : ""}`;

    fetch(url)
      .then((res) => {
        if (!res.ok) return [];
        return res.json();
      })
      .then((data) => setTraces(data))
      .catch(() => setTraces([]))
      .finally(() => setLoading(false));
  }, [filters?.error_only, filters?.limit]);

  return { traces, loading };
}
