"use client";

import Link from "next/link";
import { useState } from "react";
import { GradeControls } from "@/components/molecules/GradeControls";
import { useQueueReview } from "@/hooks/use-queue-review";
import { useReviewQueue } from "@/hooks/use-review-queues";
import { selectQueueItems, selectReviewProgress, selectReviewScore } from "@/lib/review-selectors";
import type { Grade, GradeInput } from "@/types/schema";
import type { QueueReviewItem, ReviewPriceBasis, ReviewStatus } from "@/types/review-queues";

function QueueItemReview({ digest, item, pricing, onSaved }: {
  digest: string; item: QueueReviewItem; pricing: ReviewPriceBasis | null; onSaved: () => Promise<void>;
}) {
  const review = useQueueReview({ digest, item, pricing, onSaved });
  const [skipReason, setSkipReason] = useState("");
  const saveGrade = async (grade: GradeInput): Promise<Grade> => {
    await review.submit({ kind: "grade", grade });
    const response = await fetch(`/api/grades/${item.source.evaluation_id}`, { cache: "no-store" });
    if (!response.ok) throw new Error("Grade saved; reload to read its current revision");
    return response.json() as Promise<Grade>;
  };
  const { post, evaluation, context } = item.recorded;
  const score = selectReviewScore(item.score);
  return (
    <section className="space-y-4 border-t border-gray-300 py-4 dark:border-gray-700">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <span>Evaluation #{evaluation.id} · {item.status.replaceAll("_", " ")}</span>
        <span>Ranked position: {item.source.ranked_position ?? "—"} · Random position: {item.source.random_position ?? "—"}</span>
        <span>Active review: {(review.elapsedMs / 1000).toFixed(0)}s</span>
        <button type="button" onClick={review.togglePause} disabled={review.busy || review.hasPending} className="text-blue-600 dark:text-blue-400">{review.paused ? "Resume timer" : "Pause timer"}</button>
      </div>
      <p className="text-xs text-gray-500">Timing pauses after 60 seconds idle, in hidden tabs, and during submission. Queue review only saves grades; it does not generate drafts.</p>
      <p className="text-sm text-gray-500">{post?.author_name} · {post?.channel_name}</p>
      {post?.parent_text && <blockquote className="border-l-2 border-gray-400 pl-3 text-sm">{post.parent_author_name}: {post.parent_text}</blockquote>}
      <p className="whitespace-pre-wrap text-sm">{post?.content}</p>
      <p className="text-sm"><span className="font-medium">Recorded rejection:</span> {evaluation.reason ?? "No explanation recorded"}</p>
      {score && <div className="text-sm">
        <p>{score.summary}</p>
        <p>{score.explanation}</p>
      </div>}
      <details className="text-sm">
        <summary className="cursor-pointer">Recorded dossier {evaluation.dossier_summary_id} · {evaluation.dossier_revision?.slice(0, 12)}</summary>
        <pre className="max-h-80 overflow-auto whitespace-pre-wrap py-3 text-xs">{JSON.stringify(context, null, 2)}</pre>
      </details>
      <div className="flex flex-wrap gap-3 text-sm text-blue-600 dark:text-blue-400">
        {evaluation.scan_id !== null && <Link href={`/scans/${evaluation.scan_id}`}>Source scan #{evaluation.scan_id}</Link>}
        {item.grade && <Link href={`/feedback/grades/${item.grade.id}`}>Grade #{item.grade.id} · revision #{item.current_revision_id}</Link>}
      </div>
      {item.status === "graded_elsewhere" && <div className="space-y-2 text-sm">
        <p>A grade was saved outside this queue or revised later. Link the current revision without claiming review time.</p>
        <button type="button" disabled={review.busy || review.hasPending || !review.ready} onClick={() => void review.submit({ kind: "reconcile" }).catch(() => undefined)} className="rounded border px-3 py-1">Reconcile current grade</button>
      </div>}
      {review.error && <p role="alert" className="text-sm text-red-600">{review.error}</p>}
      {review.hasPending && !review.busy && <button type="button" onClick={() => void review.retry().catch(() => undefined)} className="rounded border px-3 py-1 text-sm">Retry pending action (same ID and time)</button>}
      <fieldset disabled={!review.ready || review.busy || review.hasPending || review.paused} className="space-y-4 disabled:opacity-60">
        <GradeControls postId={evaluation.post_id} scanId={evaluation.scan_id} evaluationId={evaluation.id}
          predictedRelevant={Boolean(evaluation.relevant)} existingGrade={item.grade} saveGrade={saveGrade} />
        <div className="flex flex-wrap gap-2">
          <input aria-label="Skip reason" value={skipReason} onChange={(event) => setSkipReason(event.target.value)} placeholder="Reason for skipping" className="rounded border border-gray-300 bg-transparent px-2 py-1 text-sm dark:border-gray-700" />
          <button type="button" disabled={!skipReason.trim()} onClick={() => void review.submit({ kind: "skip", reason: skipReason.trim() }).catch(() => undefined)} className="rounded border px-3 py-1 text-sm">Skip without grading</button>
        </div>
      </fieldset>
      {item.grade?.relevance_judgment === "false_negative" && <p className="text-sm text-gray-500">Saved as a false negative. Draft generation remains a separate operator action in the existing model-negative workflow; it is not part of this review queue.</p>}
    </section>
  );
}

function selectPrice(rate: string, basis: string): ReviewPriceBasis | null {
  const value = Number(rate);
  return rate.trim() && Number.isFinite(value) && value >= 0 && basis.trim()
    ? { usd_per_hour: value, basis: basis.trim() } : null;
}

export function ReviewQueueWorkspace({ digest, initialEvaluationId = null }: { digest: string; initialEvaluationId?: number | null }) {
  const { detail, error, refresh } = useReviewQueue(digest);
  const [status, setStatus] = useState<ReviewStatus | "all">("all");
  const [selectedId, setSelectedId] = useState<number | null>(initialEvaluationId);
  const [rate, setRate] = useState("");
  const [basis, setBasis] = useState("");
  if (!detail) return <p>{error ?? "Loading review queue…"}</p>;
  const visible = selectQueueItems(detail.items, status);
  const selected = detail.items.find((item) => item.source.evaluation_id === selectedId) ?? visible[0] ?? null;
  const progress = selectReviewProgress(detail.items);
  const pricing = selectPrice(rate, basis);
  return <div className="space-y-4">
    <Link href="/feedback/review-queues" className="text-sm text-blue-600 dark:text-blue-400">All review queues</Link>
    <h1 className="text-2xl font-semibold">{detail.queue.project_key} · grading assistance</h1>
    <p className="break-all text-xs text-gray-500">Queue {digest}</p>
    <p className="text-sm">{progress.reviewed}/{detail.items.length} reviewed · {progress.skipped} skipped · {progress.graded_elsewhere} graded elsewhere</p>
    <p className="text-sm">Corpus-building review: {(detail.costs.elapsed_ms / 1000).toFixed(1)} seconds measured across {detail.costs.measured_action_count} distinct actions; {detail.costs.unavailable_action_count} without timing.
      {detail.costs.estimated_usd !== null && ` Priced portion: $${detail.costs.estimated_usd.toFixed(4)} (${(detail.costs.priced_elapsed_ms / 1000).toFixed(1)} seconds).`}</p>
    <p className="text-xs text-gray-500">Random membership is retained through skips. These progress counts are not population rate estimates. Review costs are not attributed to a candidate model.</p>
    <details className="text-xs"><summary>Selection run and action attribution</summary>
      <p className="break-all">Lineages: {detail.lineage_digests.join(", ")}</p>
      <p className="break-all">Measured/source actions: {detail.costs.action_ids.join(", ") || "None"}</p>
      <p>Replay or report with Scout’s analysis assistance-replay / assistance-report commands using this queue digest.</p>
    </details>
    <div className="flex flex-wrap gap-3">
      <label className="text-sm">Optional USD/hour <input type="number" min="0" step="any" value={rate} onChange={(event) => setRate(event.target.value)} className="w-24 rounded border bg-transparent p-1" /></label>
      <label className="text-sm">Pricing basis <input value={basis} onChange={(event) => setBasis(event.target.value)} className="rounded border bg-transparent p-1" /></label>
      {rate && pricing === null && <p className="text-sm text-amber-700">No price will be recorded until a valid rate and basis are supplied.</p>}
    </div>
    <div className="flex flex-wrap gap-3">
      <label className="text-sm">Status <select value={status} onChange={(event) => { setStatus(event.target.value as ReviewStatus | "all"); setSelectedId(null); }} className="rounded border bg-white p-1 dark:bg-gray-900">
        {["all", "pending", "reviewed", "skipped", "graded_elsewhere", "needs_regrade"].map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}
      </select></label>
      <label className="text-sm">Evaluation <select value={selected?.source.evaluation_id ?? ""} onChange={(event) => setSelectedId(Number(event.target.value))} className="rounded border bg-white p-1 dark:bg-gray-900">
        {visible.map((item) => <option key={item.source.evaluation_id} value={item.source.evaluation_id}>#{item.source.evaluation_id} · {item.status.replaceAll("_", " ")}</option>)}
      </select></label>
      <button type="button" onClick={() => void refresh().catch(() => undefined)} className="text-sm text-blue-600 dark:text-blue-400">Reload current revisions</button>
    </div>
    {selected ? <QueueItemReview key={selected.source.evaluation_id} digest={digest} item={selected} pricing={pricing} onSaved={refresh} /> : <p>No items match this filter.</p>}
  </div>;
}
