"use client";

import { useState, useCallback, useMemo } from "react";
import { useDrafts } from "@/hooks/use-drafts";
import { useNegativeGradingCases } from "@/hooks/use-negative-grading-cases";
import { DraftList } from "@/components/organisms/DraftList";
import { NegativeCaseList } from "@/components/organisms/NegativeCaseList";
import { overlayGradesByEvaluation } from "@/lib/grade-overlay";
import type { DraftWithGrade, Grade } from "@/types/schema";

type GradingView = "drafts" | "negative-cases";

function DraftGradingSection() {
  const {
    drafts: fetchedDrafts,
    filters,
    setFilters,
    loading,
    error,
    hasMore,
    loadMore,
    isLoadingMore,
  } = useDrafts({ include_grades: "true" });

  // Grade overlay: evaluation_id → Grade. Keeps local grade state independent
  // of server data so filter changes, load-more, and refetches don't
  // discard immediate feedback the operator just clicked. Keyed by
  // evaluation rather than post so a post with evaluations across
  // multiple scans only repaints the draft that was actually graded.
  const [gradeOverlay, setGradeOverlay] = useState<Map<number, Grade>>(
    () => new Map()
  );

  const drafts = useMemo<DraftWithGrade[]>(() => {
    return overlayGradesByEvaluation(fetchedDrafts as DraftWithGrade[], gradeOverlay);
  }, [fetchedDrafts, gradeOverlay]);

  const handleGradeUpdate = useCallback((evaluationId: number, grade: Grade) => {
    if (grade.evaluation_id !== evaluationId) return;
    setGradeOverlay((prev) => {
      const next = new Map(prev);
      next.set(evaluationId, grade);
      return next;
    });
  }, []);

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">Loading...</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {error && (
        <div className="rounded-lg border border-red-300 bg-red-100 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          Failed to load drafts: {error}
        </div>
      )}
      <DraftList
        drafts={drafts}
        filters={filters}
        onFilterChange={setFilters}
        onGradeUpdate={handleGradeUpdate}
        hasMore={hasMore}
        onLoadMore={loadMore}
        isLoadingMore={isLoadingMore}
      />
    </div>
  );
}

function NegativeCaseGradingSection() {
  const {
    evaluations,
    loading,
    error,
    hasMore,
    loadMore,
    isLoadingMore,
  } = useNegativeGradingCases();
  const [reviewedIds, setReviewedIds] = useState<Set<number>>(() => new Set());

  const visibleEvaluations = useMemo(
    () => evaluations.filter((evaluation) => !reviewedIds.has(evaluation.id)),
    [evaluations, reviewedIds]
  );

  const handleGradeUpdate = useCallback((evaluationId: number, grade: Grade) => {
    if (grade.evaluation_id !== evaluationId) return;
    setReviewedIds((previous) => new Set(previous).add(evaluationId));
  }, []);

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">Loading negative cases...</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {error && (
        <div className="rounded-lg border border-red-300 bg-red-100 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          Failed to load negative cases: {error}
        </div>
      )}
      <NegativeCaseList
        evaluations={visibleEvaluations}
        reviewedThisSession={reviewedIds.size}
        onGradeUpdate={handleGradeUpdate}
        hasMore={hasMore}
        onLoadMore={loadMore}
        isLoadingMore={isLoadingMore}
      />
    </div>
  );
}

export default function GradingPage() {
  const [view, setView] = useState<GradingView>("drafts");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Grading</h1>
        <div className="mt-4 flex gap-2" role="group" aria-label="Grading sections">
          <button
            type="button"
            aria-pressed={view === "drafts"}
            onClick={() => setView("drafts")}
            className={`rounded-md border px-3 py-1.5 text-sm font-medium transition-colors ${
              view === "drafts"
                ? "border-blue-300 bg-blue-100 text-blue-700 dark:border-blue-500/40 dark:bg-blue-500/20 dark:text-blue-300"
                : "border-gray-300 bg-gray-50 text-gray-600 hover:border-gray-400 hover:text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-400 dark:hover:border-gray-600 dark:hover:text-gray-200"
            }`}
          >
            Drafts
          </button>
          <button
            type="button"
            aria-pressed={view === "negative-cases"}
            onClick={() => setView("negative-cases")}
            className={`rounded-md border px-3 py-1.5 text-sm font-medium transition-colors ${
              view === "negative-cases"
                ? "border-blue-300 bg-blue-100 text-blue-700 dark:border-blue-500/40 dark:bg-blue-500/20 dark:text-blue-300"
                : "border-gray-300 bg-gray-50 text-gray-600 hover:border-gray-400 hover:text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-400 dark:hover:border-gray-600 dark:hover:text-gray-200"
            }`}
          >
            Negative Cases
          </button>
        </div>
      </div>

      {view === "drafts" ? <DraftGradingSection /> : <NegativeCaseGradingSection />}
    </div>
  );
}
