"use client";

import { useState } from "react";
import Link from "next/link";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { DraftWithGrade, Grade } from "@/types/schema";
import { Badge } from "@/components/atoms/Badge";
import { ScoreBar } from "@/components/atoms/ScoreBar";
import { ExternalLink } from "@/components/atoms/ExternalLink";
import { GradeControls } from "@/components/molecules/GradeControls";
import { MatchedRouteSummary } from "@/components/molecules/MatchedRouteSummary";
import { BlockAuthorButton } from "@/components/molecules/BlockAuthorButton";
import { truncateContent, formatTimestamp } from "@/lib/transforms";
import { GRADE_COLORS } from "@/lib/design-tokens";

interface DraftCardProps {
  draft: DraftWithGrade;
  onGradeUpdate?: (evaluationId: number, grade: Grade) => void;
}

export function DraftCard({ draft, onGradeUpdate }: DraftCardProps) {
  const [expanded, setExpanded] = useState(false);
  const evaluationId = draft.evaluation_id;
  const verdict = draft.verdict ?? "pending";
  const gradeJudgment = draft.grade?.relevance_judgment ?? "ungraded";
  const borderClass = GRADE_COLORS[gradeJudgment] ?? "";
  const gradeBorder = borderClass ? `border-l-4 ${borderClass}` : "";
  const isGateBlocked = draft.surface_status === "gate_blocked";

  return (
    <div className={`rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900 ${gradeBorder}`}>
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center justify-between p-4 text-left"
      >
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
          {expanded ? (
            <ChevronDown className="h-4 w-4 shrink-0 text-gray-600 dark:text-gray-500" />
          ) : (
            <ChevronRight className="h-4 w-4 shrink-0 text-gray-600 dark:text-gray-500" />
          )}
          <Badge label={verdict} variant="verdict" />
          {isGateBlocked && (
            <Badge label="gate-blocked" variant="platform" />
          )}
          {draft.posture && !isGateBlocked && (
            <Badge label={draft.posture} variant="project" />
          )}
          {draft.project_key && (
            <Badge label={draft.project_key} variant="project" />
          )}
          <Badge label={draft.platform} variant="platform" />
          <span className="text-sm text-gray-600 dark:text-gray-400">
            re: {draft.author_name ?? "unknown"}
          </span>
        </div>
        {draft.score !== null && (
          <div className="shrink-0">
            <ScoreBar score={draft.score} />
          </div>
        )}
      </button>

      {expanded && (
        <div className="border-t border-gray-200 dark:border-gray-800 p-4 space-y-4">
          <div>
            <h4 className="mb-1 text-xs font-medium uppercase text-gray-600 dark:text-gray-500">
              Draft Comment
            </h4>
            {isGateBlocked ? (
              <p className="text-sm italic text-gray-600 dark:text-gray-500">
                blocked by gate \u2014 text withheld
              </p>
            ) : (
              <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
                {draft.comment_text ?? "\u2014"}
              </p>
            )}
          </div>

          {draft.feedback && (
            <div>
              <h4 className="mb-1 text-xs font-medium uppercase text-gray-600 dark:text-gray-500">
                Critique Feedback
              </h4>
              <p className="text-sm text-gray-600 dark:text-gray-400 whitespace-pre-wrap">
                {draft.feedback}
              </p>
            </div>
          )}

          <div className="rounded-md border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950 p-3">
            <div className="mb-1 flex items-center justify-between gap-3">
              <h4 className="text-xs font-medium uppercase text-gray-600 dark:text-gray-500">
                Original Post
              </h4>
              <BlockAuthorButton
                platform={draft.platform}
                authorId={draft.author_id}
                authorName={draft.author_name}
              />
            </div>
            <p className="text-sm text-gray-600 dark:text-gray-400">
              {draft.content ? truncateContent(draft.content, 300) : "\u2014"}
            </p>
            {draft.url && (
              <div className="mt-2">
                <ExternalLink href={draft.url}>View original</ExternalLink>
              </div>
            )}
          </div>

          <MatchedRouteSummary route={draft.matched_route} />

          {evaluationId !== undefined && draft.scan_id && (
            <div className="border-t border-gray-200 dark:border-gray-800 pt-4">
              <div className="mb-2 flex items-center justify-between gap-2">
                <h4 className="text-xs font-medium uppercase text-gray-600 dark:text-gray-500">Grade</h4>
                <div className="flex items-center gap-3">
                  {draft.grade && (
                    <Link
                      href={`/feedback/grades/${draft.grade.id}`}
                      className="text-xs text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
                    >
                      View Grade
                    </Link>
                  )}
                  <Link
                    href={`/feedback?scanId=${draft.scan_id}`}
                    className="text-xs text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
                  >
                    View in Feedback
                  </Link>
                </div>
              </div>
              {draft.grade?.revision_count !== undefined && (
                <p className="mb-2 text-xs text-gray-600 dark:text-gray-500">
                  Saved {formatTimestamp(draft.grade.latest_recorded_at ?? null)} ·{" "}
                  {draft.grade.revision_count}{" "}
                  {draft.grade.revision_count === 1 ? "revision" : "revisions"}
                </p>
              )}
              <GradeControls
                postId={draft.post_id}
                scanId={draft.scan_id}
                evaluationId={evaluationId}
                predictedRelevant={draft.relevant}
                existingGrade={draft.grade}
                draftComment={draft.comment_text}
                onGradeChange={(grade) =>
                  onGradeUpdate?.(evaluationId, grade)
                }
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
