import { ScoreBar } from "@/components/atoms/ScoreBar";
import type { ReplyEvidence } from "@/types/feedback-experiments";

function text(value: unknown) {
  const rendered = JSON.stringify(value, null, 2);
  return rendered === undefined ? String(value) : rendered;
}

export function ReplyComparisonViewer({ evidence }: { evidence: ReplyEvidence }) {
  if (!evidence.available) return <section aria-label="Reply evidence" className="rounded-lg border p-4"><h2 className="font-semibold">Reply evidence</h2><p className="mt-1 text-sm text-gray-500">Unavailable: {evidence.reason.replaceAll("_", " ")}.</p></section>;
  const max = Math.max(evidence.baseline_distance, evidence.candidate_distance, 1);
  return <section aria-labelledby="reply-comparison-title" className="rounded-lg border p-4">
    <h2 id="reply-comparison-title" className="font-semibold">Reply evidence comparison</h2>
    <p className="mt-1 text-sm text-gray-500">Candidate minus baseline distance: {evidence.delta >= 0 ? "+" : ""}{evidence.delta}</p>
    <div className="mt-4 grid gap-4 lg:grid-cols-3">
      <article><h3 className="text-sm font-semibold">Baseline output</h3><ScoreBar value={evidence.baseline_distance} max={max} label="Baseline correction distance" /><pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-50 p-3 text-xs dark:bg-gray-900">{text(evidence.baseline_output)}</pre></article>
      <article><h3 className="text-sm font-semibold">Candidate output</h3><ScoreBar value={evidence.candidate_distance} max={max} label="Candidate correction distance" /><pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-50 p-3 text-xs dark:bg-gray-900">{text(evidence.candidate_output)}</pre></article>
      <article><h3 className="text-sm font-semibold">Pinned correction</h3><pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-50 p-3 text-xs dark:bg-gray-900">{evidence.correction}</pre></article>
    </div>
  </section>;
}
