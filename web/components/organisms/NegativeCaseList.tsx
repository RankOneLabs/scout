"use client";

import { EvaluationCard } from "@/components/organisms/EvaluationCard";
import type { Grade, ReviewEvaluation } from "@/types/schema";

interface NegativeCaseListProps {
  evaluations: ReviewEvaluation[];
  reviewedThisSession: number;
  onGradeUpdate: (evaluationId: number, grade: Grade) => void;
  hasMore: boolean;
  onLoadMore: () => void;
  isLoadingMore: boolean;
}

export function NegativeCaseList({
  evaluations,
  reviewedThisSession,
  onGradeUpdate,
  hasMore,
  onLoadMore,
  isLoadingMore,
}: NegativeCaseListProps) {
  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-gray-200 bg-gray-50 p-4 dark:border-gray-800 dark:bg-gray-900">
        <h2 className="text-sm font-medium text-gray-800 dark:text-gray-200">Model-negative cases needing review</h2>
        <p className="mt-1 text-xs text-gray-600 dark:text-gray-500">
          Recent cases appear first. Choose No when Scout was right to skip the post, or Yes to
          record a false negative and send it through the normal draft-and-critic response flow.
        </p>
        {reviewedThisSession > 0 && (
          <p className="mt-2 text-xs text-green-600 dark:text-green-400">
            Reviewed this session: {reviewedThisSession}
          </p>
        )}
      </div>

      {evaluations.length === 0 ? (
        <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">
          No negative cases need review in this page of the queue.
        </p>
      ) : (
        <div className="space-y-3">
          {evaluations.map((evaluation) => (
            <EvaluationCard
              key={evaluation.id}
              evaluation={evaluation}
              onGradeUpdate={onGradeUpdate}
            />
          ))}
        </div>
      )}

      {hasMore && (
        <button
          type="button"
          onClick={onLoadMore}
          disabled={isLoadingMore}
          className="w-full rounded-lg border border-gray-300 py-2 text-sm text-gray-600 transition-colors hover:border-gray-500 hover:text-gray-800 disabled:opacity-50 dark:border-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
        >
          {isLoadingMore ? "Loading..." : "Load more"}
        </button>
      )}
    </div>
  );
}
