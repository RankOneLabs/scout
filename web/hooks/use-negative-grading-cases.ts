"use client";

import { useCallback } from "react";
import { useFilteredList } from "@/hooks/use-filtered-list";
import type { ReviewEvaluation } from "@/types/schema";

export function useNegativeGradingCases() {
  const getEvaluationId = useCallback(
    (evaluation: ReviewEvaluation) => evaluation.id,
    []
  );
  const { items, loading, error, hasMore, loadMore, isLoadingMore } =
    useFilteredList<ReviewEvaluation>({
      path: "/api/grading/negative-cases",
      getLastId: getEvaluationId,
    });

  return {
    evaluations: items,
    loading,
    error,
    hasMore,
    loadMore,
    isLoadingMore,
  };
}
