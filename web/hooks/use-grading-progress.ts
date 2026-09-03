"use client";

import { useState, useEffect, useCallback } from "react";
import type { GradingProgress } from "@/types/schema";

export function useGradingProgress(scanId: number | null) {
  const [progress, setProgress] = useState<GradingProgress>({
    graded: 0,
    total: 0,
  });

  const refetch = useCallback(() => {
    if (scanId === null) return;
    fetch(`/api/scans/${scanId}/grading-progress`)
      .then((res) => {
        if (!res.ok) return { graded: 0, total: 0 };
        return res.json();
      })
      .then((data: GradingProgress) => setProgress(data))
      .catch(() => {});
  }, [scanId]);

  useEffect(() => {
    refetch();
  }, [refetch]);

  return { progress, refetch };
}
