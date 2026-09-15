"use client";

import Link from "next/link";
import {
  MessageSquare,
  Target,
  FileText,
  CheckCircle2,
  XCircle,
  RefreshCw,
  ShieldAlert,
  ClipboardList,
} from "lucide-react";
import type {
  ScanDetailWithCounts,
  ScanFetchFailure,
  ScanStatus,
  PostWithEvaluation,
  ReviewEvaluation,
  Grade,
  GradingProgress,
  SourceCheckpoint,
  RecoveryOperation,
} from "@/types/schema";
import { StatCard } from "@/components/molecules/StatCard";
import { GradingProgress as GradingProgressDisplay } from "@/components/molecules/GradingProgress";
import { PostList } from "@/components/organisms/PostList";
import { EvaluationList } from "@/components/organisms/EvaluationList";
import {
  formatTimestamp,
  formatDuration,
  describeCoverage,
  partitionFailuresByBlocking,
  isWatermarkStale,
  type CoverageTone,
} from "@/lib/transforms";

interface ScanDetailViewProps {
  scan: ScanDetailWithCounts;
  posts: PostWithEvaluation[];
  evaluations: ReviewEvaluation[];
  postFilters: Record<string, string>;
  onPostFilterChange: (key: string, value: string) => void;
  gradingProgress?: GradingProgress;
  onGradeUpdate?: (evaluationId: number, grade: Grade) => void;
}

const STATUS_STYLES: Record<ScanStatus, string> = {
  complete: "bg-green-100 text-green-700 border-green-300 dark:bg-green-900/50 dark:text-green-400 dark:border-green-800",
  partial: "bg-yellow-100 text-yellow-700 border-yellow-300 dark:bg-yellow-900/50 dark:text-yellow-400 dark:border-yellow-800",
  failed: "bg-red-100 text-red-700 border-red-300 dark:bg-red-900/50 dark:text-red-400 dark:border-red-800",
  interrupted: "bg-gray-100 text-gray-700 border-gray-300 dark:bg-gray-800 dark:text-gray-400 dark:border-gray-700",
};

function StatusBadge({ status }: { status: ScanStatus | null }) {
  if (!status) return null;
  const cls = STATUS_STYLES[status] ?? "bg-gray-100 text-gray-700 border-gray-300 dark:bg-gray-800 dark:text-gray-400 dark:border-gray-700";
  return (
    <span className={`rounded border px-2 py-0.5 text-xs font-medium ${cls}`}>
      {status}
    </span>
  );
}

const COVERAGE_TONE_STYLES: Record<CoverageTone, string> = {
  ok: "bg-green-100 text-green-700 border-green-300 dark:bg-green-900/50 dark:text-green-400 dark:border-green-800",
  warn: "bg-yellow-100 text-yellow-700 border-yellow-300 dark:bg-yellow-900/50 dark:text-yellow-400 dark:border-yellow-800",
  danger: "bg-red-100 text-red-700 border-red-300 dark:bg-red-900/50 dark:text-red-400 dark:border-red-800",
  neutral: "bg-gray-100 text-gray-700 border-gray-300 dark:bg-gray-800 dark:text-gray-400 dark:border-gray-700",
};

/** Coverage outcome is a distinct, independently-persisted fact from
 * processing `status` — always rendered alongside StatusBadge, never
 * collapsed into it. */
function CoverageBadge({ scan }: { scan: Pick<ScanDetailWithCounts, "coverage_outcome" | "watermark_advanced" | "role"> }) {
  const { label, tone } = describeCoverage(scan);
  return (
    <span className={`rounded border px-2 py-0.5 text-xs font-medium ${COVERAGE_TONE_STYLES[tone]}`}>
      {label}
    </span>
  );
}

function FailureRow({ f }: { f: ScanFetchFailure }) {
  return (
    <tr className="border-t border-gray-200 dark:border-gray-800 text-xs">
      <td className="py-2 pr-4 font-mono text-gray-700 dark:text-gray-300">{f.id}</td>
      <td className="py-2 pr-4 font-mono text-gray-700 dark:text-gray-300">{f.platform}</td>
      <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">{f.context ?? "—"}</td>
      <td className="py-2 pr-4">
        <span className="rounded bg-gray-100 dark:bg-gray-800 px-1.5 py-0.5 font-mono text-red-600 dark:text-red-400">
          {f.kind}
        </span>
      </td>
      <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">{f.operation_phase}</td>
      <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">{f.message ?? "—"}</td>
      <td className="py-2 pr-4 text-gray-600 dark:text-gray-500">
        {f.http_status ? (
          <span className="font-mono">{f.http_status}</span>
        ) : (
          "—"
        )}
      </td>
      <td className="py-2 pr-4 text-gray-600 dark:text-gray-500">{f.retry_after ?? "—"}</td>
      <td className="py-2 text-gray-600 dark:text-gray-500">
        {f.retryable ? (
          <span className="text-yellow-700 dark:text-yellow-500">yes</span>
        ) : (
          <span className="text-gray-500 dark:text-gray-400">no</span>
        )}
      </td>
    </tr>
  );
}

function FailureTable({ failures }: { failures: ScanFetchFailure[] }) {
  return (
    <table className="w-full text-left">
      <thead>
        <tr className="text-xs uppercase text-gray-600 dark:text-gray-500">
          <th className="py-2 pr-4">ID</th>
          <th className="py-2 pr-4">Platform</th>
          <th className="py-2 pr-4">Context</th>
          <th className="py-2 pr-4">Kind</th>
          <th className="py-2 pr-4">Phase</th>
          <th className="py-2 pr-4">Message</th>
          <th className="py-2 pr-4">HTTP</th>
          <th className="py-2 pr-4">Retry After</th>
          <th className="py-2">Retryable</th>
        </tr>
      </thead>
      <tbody>
        {failures.map((f) => (
          <FailureRow key={f.id} f={f} />
        ))}
      </tbody>
    </table>
  );
}

function SourceCheckpointsTable({ checkpoints }: { checkpoints: SourceCheckpoint[] }) {
  if (checkpoints.length === 0) return null;
  return (
    <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-950 px-4">
      <table className="w-full text-left text-xs">
        <thead>
          <tr className="uppercase text-gray-600 dark:text-gray-500">
            <th className="py-2 pr-4">Source</th>
            <th className="py-2 pr-4">Platform</th>
            <th className="py-2 pr-4">Checkpoint</th>
            <th className="py-2 pr-4">Required</th>
            <th className="py-2 pr-4">Active</th>
            <th className="py-2">Bootstrapped</th>
          </tr>
        </thead>
        <tbody>
          {checkpoints.map((c) => (
            <tr key={c.source_key} className="border-t border-gray-200 dark:border-gray-800">
              <td className="py-2 pr-4 font-mono text-gray-700 dark:text-gray-300">{c.source_key}</td>
              <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">{c.platform}</td>
              <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">
                {formatTimestamp(c.checkpoint_at)}
              </td>
              <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">{c.required ? "yes" : "no"}</td>
              <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">
                {c.active ? (
                  "yes"
                ) : (
                  <span className="text-gray-500 dark:text-gray-500">retired</span>
                )}
              </td>
              <td className="py-2 text-gray-600 dark:text-gray-400">
                {c.bootstrapped_from_legacy ? "legacy" : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RecoveryOperationRow({ op }: { op: RecoveryOperation }) {
  const outcomeCls =
    op.outcome === "accepted"
      ? "text-green-700 dark:text-green-400"
      : "text-red-700 dark:text-red-400";
  return (
    <tr className="border-t border-gray-200 dark:border-gray-800 text-xs">
      <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">{formatTimestamp(op.created_at)}</td>
      <td className="py-2 pr-4 font-mono text-gray-700 dark:text-gray-300">{op.operation}</td>
      <td className={`py-2 pr-4 font-medium ${outcomeCls}`}>{op.outcome}</td>
      <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">{op.operator}</td>
      <td className="py-2 text-gray-600 dark:text-gray-400">{op.rationale}</td>
    </tr>
  );
}

function RecoveryAndStaleness({ scan }: { scan: ScanDetailWithCounts }) {
  const lease = scan.environment_lease;
  const stale = isWatermarkStale(scan.safe_watermark_at, new Date().toISOString());
  return (
    <div className="space-y-3">
      <h3 className="text-lg font-medium text-gray-800 dark:text-gray-200">
        Watermark, Lease &amp; Recovery
      </h3>
      <div className="flex flex-wrap gap-4 rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 px-4 py-3 text-sm text-gray-700 dark:text-gray-300">
        <span>
          Safe watermark:{" "}
          <span className="font-mono">{formatTimestamp(scan.safe_watermark_at)}</span>
        </span>
        <span className={stale ? "font-medium text-red-600 dark:text-red-400" : "text-green-700 dark:text-green-400"}>
          {stale ? "stale" : "fresh"}
        </span>
        {lease && (
          <span>
            Lease: fence {lease.fence}
            {lease.owner_id ? `, held by ${lease.owner_id}` : ", unheld"}
          </span>
        )}
        {scan.latest_probe_run && (
          <span>
            Latest probe: {scan.latest_probe_run.passed ? "passed" : "failed"} (
            {scan.latest_probe_run.window_hours}h window,{" "}
            {formatTimestamp(scan.latest_probe_run.started_at)})
          </span>
        )}
      </div>
      {scan.recent_recovery_operations.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-950 px-4">
          <table className="w-full text-left">
            <thead>
              <tr className="text-xs uppercase text-gray-600 dark:text-gray-500">
                <th className="py-2 pr-4">When</th>
                <th className="py-2 pr-4">Operation</th>
                <th className="py-2 pr-4">Outcome</th>
                <th className="py-2 pr-4">Operator</th>
                <th className="py-2">Rationale</th>
              </tr>
            </thead>
            <tbody>
              {scan.recent_recovery_operations.map((op) => (
                <RecoveryOperationRow key={op.id} op={op} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function FeedbackCoverage({ feedback }: { feedback: ScanDetailWithCounts["feedback"] }) {
  if (feedback === undefined) return null;
  if (feedback === null) {
    return (
      <div className="rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 px-4 py-3 text-sm text-gray-600 dark:text-gray-500">
        Feedback snapshot unavailable for this scan.
      </div>
    );
  }
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 px-4 py-3">
      <div className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
        <ClipboardList className="h-4 w-4 text-purple-600 dark:text-purple-400" />
        <span>
          Feedback snapshot #{feedback.snapshot_id} ({feedback.mode}, {feedback.policy_version}):{" "}
          {feedback.population_count} stored, {feedback.eligible_count} eligible,{" "}
          {feedback.excluded_count} excluded
        </span>
      </div>
      <Link
        href={`/feedback?snapshotId=${feedback.snapshot_id}`}
        className="text-sm text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300"
      >
        View in Feedback
      </Link>
    </div>
  );
}

export function ScanDetailView({
  scan,
  posts,
  evaluations,
  postFilters,
  onPostFilterChange,
  gradingProgress,
  onGradeUpdate,
}: ScanDetailViewProps) {
  const failures = scan.failures ?? [];

  return (
    <div className="space-y-8">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
              Scan #{scan.id}
            </h2>
            <StatusBadge status={scan.status} />
            <CoverageBadge scan={scan} />
          </div>
          <p className="mt-1 text-sm text-gray-600 dark:text-gray-400">
            {formatTimestamp(scan.started_at)} &middot;{" "}
            {formatDuration(scan.started_at, scan.completed_at)}
          </p>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-500">
            {scan.environment} &middot; {scan.role} &middot; run kind: {scan.run_kind}
            {scan.canonical_scan_id !== null && (
              <>
                {" "}
                &middot;{" "}
                <Link
                  href={`/scans/${scan.canonical_scan_id}`}
                  className="text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300"
                >
                  canonical owner #{scan.canonical_scan_id}
                </Link>
              </>
            )}
          </p>
          {scan.overflow_count > 0 && (
            <p className="mt-1 text-xs text-yellow-700 dark:text-yellow-500">
              {scan.overflow_count} messages capped (cap overflow)
            </p>
          )}
        </div>
        {gradingProgress && gradingProgress.total > 0 && (
          <GradingProgressDisplay
            graded={gradingProgress.graded}
            total={gradingProgress.total}
          />
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 sm:gap-4 lg:grid-cols-7">
        <StatCard
          label="Posts"
          value={scan.post_count}
          icon={MessageSquare}
          color="text-purple-600 dark:text-purple-400"
        />
        <StatCard
          label="Evaluated"
          value={scan.eval_count}
          icon={Target}
          color="text-blue-400"
        />
        <StatCard
          label="Drafts"
          value={scan.draft_count}
          icon={FileText}
          color="text-amber-600 dark:text-amber-400"
        />
        <StatCard
          label="Approved"
          value={scan.approved_count}
          icon={CheckCircle2}
          color="text-green-600 dark:text-green-400"
        />
        <StatCard
          label="Rejected"
          value={scan.rejected_count}
          icon={XCircle}
          color="text-red-400"
        />
        <StatCard
          label="Revised"
          value={scan.revised_count}
          icon={RefreshCw}
          color="text-amber-600 dark:text-amber-400"
        />
        <StatCard
          label="Gate Blocked"
          value={scan.gate_blocked_count ?? 0}
          icon={ShieldAlert}
          color="text-orange-400"
        />
      </div>

      <FeedbackCoverage feedback={scan.feedback} />

      {failures.length > 0 && (() => {
        const { blocking, nonBlocking } = partitionFailuresByBlocking(failures);
        return (
          <div className="space-y-6">
            {blocking.length > 0 && (
              <div>
                <h3 className="mb-3 text-base font-medium text-red-600 dark:text-red-400">
                  Blocking Failures ({blocking.length}) &mdash; the exact reason coverage did not advance
                </h3>
                <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-950 px-4">
                  <FailureTable failures={blocking} />
                </div>
              </div>
            )}
            {nonBlocking.length > 0 && (
              <div>
                <h3 className="mb-3 text-base font-medium text-yellow-700 dark:text-yellow-500">
                  Non-blocking Degradation ({nonBlocking.length}) &mdash; recorded, but never blocked the watermark
                </h3>
                <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-950 px-4">
                  <FailureTable failures={nonBlocking} />
                </div>
              </div>
            )}
          </div>
        );
      })()}

      {scan.role === "canonical_live" && <RecoveryAndStaleness scan={scan} />}

      <div>
        <h3 className="mb-4 text-lg font-medium text-gray-800 dark:text-gray-200">
          Source Checkpoints
        </h3>
        <SourceCheckpointsTable checkpoints={scan.source_checkpoints} />
      </div>

      <div>
        <h3 className="mb-4 text-lg font-medium text-gray-800 dark:text-gray-200">Posts</h3>
        <PostList
          posts={posts}
          filters={postFilters}
          onFilterChange={onPostFilterChange}
        />
      </div>

      <div>
        <h3 className="mb-4 text-lg font-medium text-gray-800 dark:text-gray-200">Evaluations</h3>
        <EvaluationList evaluations={evaluations} onGradeUpdate={onGradeUpdate} />
      </div>
    </div>
  );
}
