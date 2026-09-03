"use client";

import { useState } from "react";
import Link from "next/link";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { Grade, ReviewEvaluation } from "@/types/schema";
import { Badge } from "@/components/atoms/Badge";
import { ScoreBar } from "@/components/atoms/ScoreBar";
import { ExternalLink } from "@/components/atoms/ExternalLink";
import { GradeControls } from "@/components/molecules/GradeControls";
import { MatchedRouteSummary } from "@/components/molecules/MatchedRouteSummary";
import { BlockAuthorButton } from "@/components/molecules/BlockAuthorButton";
import { truncateContent, formatTimestamp } from "@/lib/transforms";

export function EvaluationCard({ evaluation, onGradeUpdate }: {
  evaluation: ReviewEvaluation;
  onGradeUpdate?: (evaluationId: number, grade: Grade) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const surfaced = evaluation.surface_status === "surfaced";
  return <div id={`evaluation-${evaluation.id}`} className="rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900">
    <button type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)} className="flex w-full items-center justify-between p-4 text-left">
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
        {expanded ? <ChevronDown className="h-4 w-4 text-gray-600 dark:text-gray-500" /> : <ChevronRight className="h-4 w-4 text-gray-600 dark:text-gray-500" />}
        <Badge label={evaluation.surface_status.replace(/_/g, "-")} variant="verdict" />
        {evaluation.project_key && <Badge label={evaluation.project_key} variant="project" />}
        <Badge label={evaluation.post.platform} variant="platform" />
        <span className="text-xs text-gray-600 dark:text-gray-500">evaluation #{evaluation.id}</span>
        <span className="text-sm text-gray-600 dark:text-gray-400">re: {evaluation.post.author_name ?? "unknown"}</span>
      </div>
      <ScoreBar score={evaluation.score} />
    </button>
    {expanded && <div className="space-y-4 border-t border-gray-200 dark:border-gray-800 p-4">
      {surfaced && evaluation.draft ? <div>
        <h4 className="mb-1 text-xs font-medium uppercase text-gray-600 dark:text-gray-500">Approved reply</h4>
        <p className="whitespace-pre-wrap text-sm text-gray-700 dark:text-gray-300">{evaluation.draft.comment_text}</p>
      </div> : evaluation.surface_status === "gate_blocked" ? <div>
        <h4 className="mb-1 text-xs font-medium uppercase text-orange-700 dark:text-orange-400">Gate-blocked evidence</h4>
        {evaluation.gate_violations.map((v) => <div key={v.id} className="mb-2 rounded border border-orange-300 bg-orange-100 p-2 text-sm text-orange-800 dark:border-orange-900 dark:bg-orange-950/30 dark:text-orange-200">
          <span className="font-mono">{v.reason_code}</span>
          {v.offending_text && <p className="mt-1 whitespace-pre-wrap text-orange-900 dark:text-orange-100">Offending text: {v.offending_text}</p>}
        </div>)}
      </div> : <div>
        <h4 className="mb-1 text-xs font-medium uppercase text-gray-600 dark:text-gray-500">Outcome evidence</h4>
        <p className="text-sm text-gray-600 dark:text-gray-400">{evaluation.failure_reason ?? evaluation.reason ?? "No approved reply was created."}</p>
        {evaluation.critique?.feedback && <p className="mt-2 whitespace-pre-wrap text-sm text-gray-600 dark:text-gray-400">Critique: {evaluation.critique.feedback}</p>}
      </div>}
      <div className="rounded-md border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950 p-3">
        <div className="mb-1 flex items-center justify-between gap-3">
          <h4 className="text-xs font-medium uppercase text-gray-600 dark:text-gray-500">Original post</h4>
          <BlockAuthorButton
            platform={evaluation.post.platform}
            authorId={evaluation.post.author_id}
            authorName={evaluation.post.author_name}
          />
        </div>
        <p className="text-sm text-gray-600 dark:text-gray-400">{evaluation.post.content ? truncateContent(evaluation.post.content, 300) : "—"}</p>
        {evaluation.post.url && <div className="mt-2"><ExternalLink href={evaluation.post.url}>View original</ExternalLink></div>}
      </div>
      <MatchedRouteSummary route={evaluation.matched_route} />
      {evaluation.scan_id && <div className="border-t border-gray-200 dark:border-gray-800 pt-4">
        <div className="mb-2 flex items-center justify-between gap-2">
          <h4 className="text-xs font-medium uppercase text-gray-600 dark:text-gray-500">Grade</h4>
          <div className="flex items-center gap-3">
            {evaluation.grade && (
              <Link href={`/feedback/grades/${evaluation.grade.id}`} className="text-xs text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300">
                View Grade
              </Link>
            )}
            <Link href={`/feedback?scanId=${evaluation.scan_id}`} className="text-xs text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300">
              View in Feedback
            </Link>
          </div>
        </div>
        {evaluation.grade?.revision_count !== undefined && (
          <p className="mb-2 text-xs text-gray-600 dark:text-gray-500">
            Saved {formatTimestamp(evaluation.grade.latest_recorded_at ?? null)} · {evaluation.grade.revision_count}{" "}
            {evaluation.grade.revision_count === 1 ? "revision" : "revisions"}
          </p>
        )}
        <GradeControls postId={evaluation.post_id} scanId={evaluation.scan_id} evaluationId={evaluation.id} predictedRelevant={evaluation.relevant} existingGrade={evaluation.grade} draftComment={evaluation.draft?.comment_text} onGradeChange={(grade) => onGradeUpdate?.(evaluation.id, grade)} />
      </div>}
    </div>}
  </div>;
}
