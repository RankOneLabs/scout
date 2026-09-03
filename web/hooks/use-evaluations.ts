"use client";

import { useEffect, useState } from "react";
import type { ReviewEvaluation } from "@/types/schema";

type EvaluationResult = {
  scanId: number;
  evaluations: ReviewEvaluation[];
  error: string | null;
};

export function useEvaluations(scanId: number | null) {
  const [result, setResult] = useState<EvaluationResult | null>(null);

  useEffect(() => {
    if (scanId === null) return;

    let cancelled = false;
    fetch(`/api/evaluations?scan_id=${scanId}`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<ReviewEvaluation[]>;
      })
      .then((items) => {
        if (!cancelled) setResult({ scanId, evaluations: items, error: null });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setResult({
            scanId,
            evaluations: [],
            error: err instanceof Error ? err.message : "request failed",
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [scanId]);

  if (scanId === null) {
    return { evaluations: [], loading: false, error: null };
  }
  const isCurrentResult = result?.scanId === scanId;
  return {
    evaluations: isCurrentResult ? result.evaluations : [],
    loading: !isCurrentResult,
    error: isCurrentResult ? result.error : null,
  };
}
