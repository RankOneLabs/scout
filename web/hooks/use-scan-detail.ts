"use client";

import { useState, useEffect } from "react";
import type { ScanDetailWithCounts } from "@/types/schema";

export function useScanDetail(id: number) {
  const [scan, setScan] = useState<ScanDetailWithCounts | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`/api/scans?id=${id}`)
      .then((res) => {
        if (res.status === 404) return null;
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<ScanDetailWithCounts>;
      })
      .then((data) => {
        setScan(data);
        setError(null);
      })
      .catch((err: unknown) => {
        setScan(null);
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => setLoading(false));
  }, [id]);

  return { scan, loading, error };
}
