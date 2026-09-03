"use client";

import { useState, useCallback, useMemo } from "react";
import { useParams } from "next/navigation";
import { useScanDetail } from "@/hooks/use-scan-detail";
import { usePosts } from "@/hooks/use-posts";
import { useEvaluations } from "@/hooks/use-evaluations";
import { useGradingProgress } from "@/hooks/use-grading-progress";
import { ScanDetailView } from "@/components/organisms/ScanDetailView";
import { parseIdParam } from "@/lib/route-utils";
import type { Grade, ReviewEvaluation } from "@/types/schema";

export default function ScanDetailPage() {
  const params = useParams<{ id: string }>();
  const scanId = parseIdParam(params.id);

  if (scanId === null) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">Invalid scan id.</p>
      </div>
    );
  }

  return <ScanDetailContent scanId={scanId} />;
}

function ScanDetailContent({ scanId }: { scanId: number }) {
  const { scan, loading: scanLoading, error: scanError } = useScanDetail(scanId);
  const {
    posts,
    filters: postFilters,
    setFilters: setPostFilters,
    loading: postsLoading,
    error: postsError,
  } = usePosts({ scan_id: String(scanId) });
  const { evaluations: fetchedEvaluations, loading: evaluationsLoading, error: evaluationsError } = useEvaluations(scanId);
  const { progress, refetch: refetchProgress } = useGradingProgress(scanId);

  const [gradeOverlay, setGradeOverlay] = useState<Map<number, Grade>>(
    () => new Map()
  );

  const evaluations = useMemo<ReviewEvaluation[]>(() => fetchedEvaluations.map((evaluation) => {
    const grade = gradeOverlay.get(evaluation.id);
    return grade ? { ...evaluation, grade } : evaluation;
  }), [fetchedEvaluations, gradeOverlay]);

  const handleGradeUpdate = useCallback(
    (evaluationId: number, grade: Grade) => {
      setGradeOverlay((prev) => {
        const next = new Map(prev);
        next.set(evaluationId, grade);
        return next;
      });
      refetchProgress();
    },
    [refetchProgress]
  );

  const loading = scanLoading || postsLoading || evaluationsLoading;

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">Loading...</p>
      </div>
    );
  }

  if (scanError || !scan) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">
          {scanError ? `Failed to load scan: ${scanError}` : "Scan not found."}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {(postsError || evaluationsError) && (
        <div className="rounded-lg border border-red-300 bg-red-100 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          {postsError && <p>Failed to load posts: {postsError}</p>}
          {evaluationsError && <p>Failed to load evaluations: {evaluationsError}</p>}
        </div>
      )}
      <ScanDetailView
        scan={scan}
        posts={posts}
        evaluations={evaluations}
        postFilters={postFilters}
        onPostFilterChange={setPostFilters}
        gradingProgress={progress}
        onGradeUpdate={handleGradeUpdate}
      />
    </div>
  );
}
