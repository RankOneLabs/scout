"use client";

import type { Grade, ReviewEvaluation } from "@/types/schema";
import { EvaluationCard } from "@/components/organisms/EvaluationCard";

export function EvaluationList({ evaluations, onGradeUpdate }: {
  evaluations: ReviewEvaluation[];
  onGradeUpdate?: (evaluationId: number, grade: Grade) => void;
}) {
  if (!evaluations.length) return <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">No evaluations found.</p>;
  return <div className="space-y-3">{evaluations.map((evaluation) => <EvaluationCard key={evaluation.id} evaluation={evaluation} onGradeUpdate={onGradeUpdate} />)}</div>;
}
