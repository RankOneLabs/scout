"use client";

import { useCallback } from "react";

import { useFilteredList } from "@/hooks/use-filtered-list";
import type { ReviewEvaluation } from "@/types/schema";

export function useReviewGradingCases() {
  const getEvaluationId = useCallback(
    (evaluation: ReviewEvaluation) => evaluation.id,
    []
  );
  const { items, loading, error, hasMore, loadMore, isLoadingMore } =
    useFilteredList<ReviewEvaluation>({
      path: "/api/grading/review-cases",
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
