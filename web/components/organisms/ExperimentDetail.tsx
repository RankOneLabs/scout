"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { useExperimentDetail } from "@/hooks/use-experiment-detail";
import { DetailField } from "@/components/atoms/DetailField";
import { ExperimentMetricExplainer } from "@/components/molecules/ExperimentMetricExplainer";
import { ReplyComparisonViewer } from "@/components/molecules/ReplyComparisonViewer";
import { parseUtc } from "@/lib/transforms";
import {
  computeAttemptVerdict,
  computeDecisionMetrics,
  type AttemptVerdictKind,
} from "@/lib/experiment-presentation";
import type {
  BaselineEvidence,
  DomainDiff,
  DomainDiffSide,
  ExperimentComparison,
  ExperimentRunStatus,
  ExperimentStatus,
  ScoreEvidence,
  ToolEvent,
} from "@/types/feedback-experiments";

function fmt(iso: string | null): string {
  if (!iso) return "—";
  const d = parseUtc(iso);
  return Number.isNaN(d.valueOf()) ? iso : d.toLocaleString();
}

const STATUS_COLORS: Record<ExperimentStatus, string> = {
  queued: "border-gray-300 bg-white text-gray-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400",
  running: "border-blue-300 bg-blue-100 text-blue-700 dark:border-blue-500/30 dark:bg-blue-500/20 dark:text-blue-400",
  complete: "border-green-300 bg-green-100 text-green-700 dark:border-green-500/30 dark:bg-green-500/20 dark:text-green-400",
  failed: "border-red-300 bg-red-100 text-red-700 dark:border-red-500/30 dark:bg-red-500/20 dark:text-red-400",
};

const RUN_STATUS_COLORS: Record<ExperimentRunStatus, string> = {
  ...STATUS_COLORS,
  partial: "border-amber-300 bg-amber-100 text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/20 dark:text-amber-400",
};

const VERDICT_STYLES: Record<AttemptVerdictKind, string> = {
  pending: "border-blue-300 bg-blue-50 text-blue-800 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-300",
  failed: "border-red-300 bg-red-50 text-red-800 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-300",
  no_comparison: "border-gray-300 bg-gray-50 text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300",
  not_graded: "border-gray-300 bg-gray-50 text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300",
  candidate_recommended:
    "border-green-300 bg-green-50 text-green-800 dark:border-green-500/30 dark:bg-green-500/10 dark:text-green-300",
  candidate_not_recommended:
    "border-red-300 bg-red-50 text-red-800 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-300",
  no_measurable_difference:
    "border-gray-300 bg-gray-50 text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300",
};

function Unavailable() {
  return <span className="italic text-gray-500 dark:text-gray-400">Unavailable</span>;
}

// A native <summary> already carries disclosure semantics, but replacing a
// section's old <h2> with one drops it from screen-reader heading
// navigation. Exposing the label as an aria heading restores that without
// giving up the native, JS-free expand/collapse behavior.
function SectionSummary({ children }: { children: ReactNode }) {
  return (
    <summary className="disclosure-summary mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">
      <span role="heading" aria-level={2}>
        {children}
      </span>
    </summary>
  );
}

function renderStructuredEvidence(value: unknown): string {
  const rendered = JSON.stringify(value, null, 2);
  return rendered === undefined ? String(value) : rendered;
}

function ToolEventEvidence({
  label,
  event,
  alignedIndex,
}: {
  label: string;
  event: ToolEvent | null;
  alignedIndex: number | null;
}) {
  if (event === null) {
    return (
      <div className="rounded border border-gray-200 dark:border-gray-800 p-2">
        <p className="font-medium text-gray-600 dark:text-gray-500">{label}</p>
        <Unavailable />
      </div>
    );
  }
  return (
    <div className="min-w-0 rounded border border-gray-200 dark:border-gray-800 p-2">
      <p className="font-medium text-gray-700 dark:text-gray-300">
        {label}: {event.name}
        <span className="ml-2 font-normal text-gray-500 dark:text-gray-400">
          trace index {alignedIndex ?? <Unavailable />}
        </span>
      </p>
      <p className="mt-2 text-[10px] font-medium uppercase text-gray-500 dark:text-gray-400">Arguments</p>
      <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-100 dark:bg-black/40 p-2 text-[11px] text-gray-600 dark:text-gray-400">
        {renderStructuredEvidence(event.args)}
      </pre>
      <p className="mt-2 text-[10px] font-medium uppercase text-gray-500 dark:text-gray-400">Output</p>
      {event.output === null ? (
        <Unavailable />
      ) : (
        <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-100 dark:bg-black/40 p-2 text-[11px] text-gray-600 dark:text-gray-400">
          {event.output}
        </pre>
      )}
      <p className="mt-2 text-[10px] font-medium uppercase text-gray-500 dark:text-gray-400">Error</p>
      {event.error === null ? (
        <Unavailable />
      ) : (
        <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-red-100 text-red-700 dark:bg-red-950/30 p-2 text-[11px] dark:text-red-300">
          {event.error}
        </pre>
      )}
    </div>
  );
}

function PromptDisclosure({
  label,
  prompt,
  sha256,
}: {
  label: string;
  prompt: string;
  sha256: string;
}) {
  return (
    <details className="mt-2">
      <summary className="disclosure-summary text-xs font-medium text-gray-600 dark:text-gray-400">{label}</summary>
      <p className="mt-1 font-mono text-[10px] text-gray-500 dark:text-gray-400">sha256:{sha256}</p>
      <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950 p-3 text-xs text-gray-700 dark:text-gray-300">
        {prompt}
      </pre>
    </details>
  );
}

function DomainDiffSideCard({ label, side }: { label: string; side: DomainDiffSide }) {
  return (
    <div className="rounded border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950 p-2 text-xs">
      <p className="font-medium text-gray-700 dark:text-gray-300">{label}</p>
      <dl className="mt-1 grid grid-cols-2 gap-2">
        <DetailField
          as="span"
          label="Complete"
          value={side.complete ? "yes" : "no"}
          valueClassName={side.complete ? "text-green-700 dark:text-green-400" : "text-amber-700 dark:text-amber-400"}
        />
        <DetailField
          as="span"
          label="Incomplete reason"
          value={side.incomplete_reason ?? <Unavailable />}
        />
        <DetailField as="span" label="SHA-256" value={side.sha256 ?? <Unavailable />} valueClassName="font-mono text-[10px] text-gray-600 dark:text-gray-400" />
        <DetailField
          as="span"
          label="UTF-8 byte length"
          value={side.utf8_byte_length ?? <Unavailable />}
        />
      </dl>
      {side.complete ? (
        <details className="mt-2">
          <summary className="disclosure-summary text-gray-600 dark:text-gray-400">Value</summary>
          <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-100 dark:bg-black/40 p-2 text-[11px] text-gray-600 dark:text-gray-400">
            {JSON.stringify(side.value, null, 2)}
          </pre>
        </details>
      ) : (
        <Unavailable />
      )}
    </div>
  );
}

function PointerList({ label, pointers, opClass }: { label: string; pointers: string[]; opClass: string }) {
  if (pointers.length === 0) return null;
  return (
    <div>
      <p className="text-[11px] font-medium text-gray-600 dark:text-gray-500">{label}</p>
      <ul className="mt-1 space-y-0.5">
        {pointers.map((pointer) => (
          <li key={pointer} className="font-mono text-[11px]">
            <span className={opClass}>{label === "Additions" ? "+" : label === "Removals" ? "-" : "~"}</span>{" "}
            <span className="text-gray-700 dark:text-gray-300">{pointer}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function DomainDiffCard({ domainDiff }: { domainDiff: DomainDiff }) {
  const bothComplete = domainDiff.baseline.complete && domainDiff.candidate.complete;
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <DomainDiffSideCard label="Baseline" side={domainDiff.baseline} />
        <DomainDiffSideCard label="Candidate" side={domainDiff.candidate} />
      </div>
      {bothComplete ? (
        <div className="space-y-2 rounded border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950 p-2">
          <PointerList label="Additions" pointers={domainDiff.additions ?? []} opClass="text-green-700 dark:text-green-400" />
          <PointerList label="Removals" pointers={domainDiff.removals ?? []} opClass="text-red-700 dark:text-red-400" />
          <PointerList label="Changes" pointers={domainDiff.changes ?? []} opClass="text-amber-700 dark:text-amber-400" />
          {(domainDiff.additions?.length ?? 0) === 0 &&
            (domainDiff.removals?.length ?? 0) === 0 &&
            (domainDiff.changes?.length ?? 0) === 0 && (
              <p className="text-xs text-gray-600 dark:text-gray-500">No field differences.</p>
            )}
        </div>
      ) : (
        <p className="text-xs italic text-amber-700 dark:text-amber-400">
          Field-level comparison unavailable — at least one side&apos;s structured output was
          incomplete.
        </p>
      )}
    </div>
  );
}

function ScoreDeltas({ comparison }: { comparison: ExperimentComparison }) {
  const dims = Object.keys(comparison.trace_diff.score_details);
  if (dims.length === 0) {
    return <p className="text-xs italic text-amber-700 dark:text-amber-400">No grader scores recorded for this comparison.</p>;
  }
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-left text-gray-600 dark:text-gray-500">
          <th className="pb-1 font-normal">Dimension</th>
          <th className="pb-1 font-normal">Baseline</th>
          <th className="pb-1 font-normal">Candidate</th>
          <th className="pb-1 font-normal">Delta</th>
        </tr>
      </thead>
      <tbody className="text-gray-700 dark:text-gray-300">
        {dims.map((dim) => {
          const [a, b] = comparison.trace_diff.score_details[dim];
          const delta = comparison.trace_diff.score_deltas[dim];
          return (
            <tr key={dim} className="border-t border-gray-200 dark:border-gray-800">
              <td className="py-1">{dim}</td>
              <td className="py-1">{a ?? <Unavailable />}</td>
              <td className="py-1">{b ?? <Unavailable />}</td>
              <td className="py-1">{delta === undefined ? <Unavailable /> : delta}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function BaselineEvidenceSection({ evidence }: { evidence: BaselineEvidence }) {
  const isGraded = evidence.reply_revision_id !== undefined;
  return (
    <details className="rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900 p-4">
      <SectionSummary>Baseline evidence (this attempt)</SectionSummary>
      <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
        <DetailField
          label="Baseline prompt reused"
          value={evidence.baseline_prompt_reused ? "yes" : "no"}
        />
        <DetailField
          label="Recorded input SHA-256"
          value={evidence.recorded_input_sha256}
          valueClassName="font-mono text-[10px] text-gray-600 dark:text-gray-400"
        />
      </dl>
      {isGraded ? (
        <div className="mt-3 rounded border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950 p-3">
          <h3 className="mb-1 text-xs font-medium uppercase text-gray-600 dark:text-gray-500">
            Pinned correction oracle
          </h3>
          <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
            <DetailField label="Project" value={evidence.project_key ?? <Unavailable />} />
            <DetailField label="Dossier summary" value={evidence.dossier_summary_id ?? <Unavailable />} />
            <DetailField
              label="Dossier revision"
              value={evidence.dossier_revision ?? <Unavailable />}
              valueClassName="font-mono text-[10px] text-gray-600 dark:text-gray-400"
            />
            <DetailField label="Reply revision" value={`#${evidence.reply_revision_id}`} />
            <DetailField label="Baseline model" value={evidence.baseline_model ?? <Unavailable />} />
            <DetailField
              label="Baseline prompt SHA-256"
              value={evidence.baseline_prompt_sha256 ?? <Unavailable />}
              valueClassName="font-mono text-[10px] text-gray-600 dark:text-gray-400"
            />
            <DetailField label="Grader version" value={evidence.grader_version ?? <Unavailable />} />
            <DetailField label="Assembler version" value={evidence.assembler_version ?? <Unavailable />} />
          </dl>
          <p className="mt-2 font-mono text-[10px] text-gray-500 dark:text-gray-400">
            correction sha256:{evidence.correction_sha256 ?? <Unavailable />}
          </p>
        </div>
      ) : (
        <p className="mt-2 text-xs italic text-gray-600 dark:text-gray-500">
          No correction oracle was pinned for this attempt (not a graded reply_draft replay).
        </p>
      )}
    </details>
  );
}

function ScoreEvidenceCard({ scoreEvidence }: { scoreEvidence: ScoreEvidence }) {
  const improved = scoreEvidence.delta < 0;
  return (
    <div className="rounded border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950 p-3 text-xs">
      <p className="font-medium text-gray-700 dark:text-gray-300">
        {scoreEvidence.grader_version} — assembled with {scoreEvidence.assembler_version}
      </p>
      <dl className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <DetailField as="span" label="Baseline distance" value={scoreEvidence.baseline_distance} />
        <DetailField as="span" label="Candidate distance" value={scoreEvidence.candidate_distance} />
        <DetailField
          as="span"
          label="Delta (candidate − baseline)"
          value={`${scoreEvidence.delta >= 0 ? "+" : ""}${scoreEvidence.delta}`}
          valueClassName={improved ? "text-green-700 dark:text-green-400" : "text-amber-700 dark:text-amber-400"}
        />
        <DetailField
          as="span"
          label="Reply revision"
          value={`#${scoreEvidence.reply_revision_id}`}
        />
      </dl>
      <p className="mt-2 font-mono text-[10px] text-gray-500 dark:text-gray-400">
        correction sha256:{scoreEvidence.correction_sha256}
      </p>
      <p className="mt-0.5 text-[10px] text-gray-600 dark:text-gray-500">
        {improved
          ? "Negative delta — the candidate is closer to the pinned correction than the baseline was."
          : "Non-negative delta — the candidate is not closer to the pinned correction than the baseline was."}
      </p>
    </div>
  );
}

function ComparisonSection({ comparison, status }: { comparison: ExperimentComparison | null; status: ExperimentStatus }) {
  if (comparison === null) {
    return (
      <details className="rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900 p-4">
        <SectionSummary>Comparison</SectionSummary>
        <p className="text-xs italic text-gray-600 dark:text-gray-500">
          No comparison has been persisted yet (status: {status}).
        </p>
      </details>
    );
  }

  const { trace_diff: td, domain_diff: dd } = comparison;

  return (
    <details className="rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900 p-4">
      <SectionSummary>Comparison</SectionSummary>
      <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
        <DetailField label="Jig revision" value={comparison.jig_revision} valueClassName="font-mono text-[10px] text-gray-600 dark:text-gray-400" />
        <DetailField label="Persisted" value={fmt(comparison.created_at)} />
        <DetailField
          label="Comparison complete"
          value={td.comparison_complete ? "yes" : "no"}
          valueClassName={td.comparison_complete ? "text-green-700 dark:text-green-400" : "text-amber-700 dark:text-amber-400"}
        />
        <DetailField label="Incomplete reason" value={td.comparison_incomplete_reason ?? "—"} />
        <DetailField
          label="Raw cost delta"
          value={
            !comparison.cost_delta_available
              ? <Unavailable />
              : `${td.cost_delta >= 0 ? "+" : ""}${td.cost_delta}`
          }
        />
        <DetailField
          label="Raw latency delta (ms)"
          value={
            !comparison.latency_delta_available
              ? <Unavailable />
              : `${td.latency_ms_delta >= 0 ? "+" : ""}${td.latency_ms_delta}`
          }
        />
        <DetailField
          label="Output hash (baseline → candidate)"
          value={
            <>
              {td.a_output_hash ?? <Unavailable />} → {td.b_output_hash ?? <Unavailable />}
            </>
          }
          valueClassName="font-mono text-[10px] text-gray-600 dark:text-gray-400"
        />
        <DetailField
          label="Output byte length (baseline → candidate)"
          value={
            <>
              {td.a_output_byte_length ?? <Unavailable />} → {td.b_output_byte_length ?? <Unavailable />}
            </>
          }
        />
      </dl>

      {td.error_category_change && (
        <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">
          Error category changed: {td.error_category_change[0] ?? "none"} →{" "}
          {td.error_category_change[1] ?? "none"}
        </p>
      )}

      <div className="mt-3">
        <h3 className="mb-1 text-xs font-medium uppercase text-gray-600 dark:text-gray-500">Grader scores</h3>
        <ScoreDeltas comparison={comparison} />
      </div>

      <div className="mt-3">
        <h3 className="mb-1 text-xs font-medium uppercase text-gray-600 dark:text-gray-500">
          Reply-correction score
        </h3>
        {comparison.score_evidence === null ? (
          <p className="text-xs italic text-gray-600 dark:text-gray-500">
            No reply-correction grader was attached to this comparison.
          </p>
        ) : (
          <ScoreEvidenceCard scoreEvidence={comparison.score_evidence} />
        )}
      </div>

      <details className="mt-3">
        <summary className="disclosure-summary text-xs font-medium uppercase text-gray-600 dark:text-gray-500">
          Tool call divergence ({td.tool_divergence.length})
        </summary>
        {td.tool_divergence.length === 0 ? (
          <p className="mt-1 text-xs text-gray-600 dark:text-gray-500">No divergent tool calls.</p>
        ) : (
          <ul className="mt-1 space-y-1">
            {td.tool_divergence.map((entry) => (
              <li key={entry.index} className="rounded border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950 p-2 text-xs">
                <span className="font-medium text-gray-700 dark:text-gray-300">{entry.divergence}</span>
                {entry.tier && <span className="ml-2 text-gray-600 dark:text-gray-500">tier: {entry.tier}</span>}
                <div className="mt-2 grid grid-cols-1 gap-2 text-[11px] text-gray-600 dark:text-gray-400 lg:grid-cols-2">
                  <ToolEventEvidence label="Baseline" event={entry.a} alignedIndex={entry.index_a} />
                  <ToolEventEvidence label="Candidate" event={entry.b} alignedIndex={entry.index_b} />
                </div>
              </li>
            ))}
          </ul>
        )}
      </details>

      <details className="mt-3">
        <summary className="disclosure-summary text-xs font-medium uppercase text-gray-600 dark:text-gray-500">
          Structured domain diff
        </summary>
        <div className="mt-1">
          <DomainDiffCard domainDiff={dd} />
        </div>
      </details>
    </details>
  );
}

function VerdictHeader({
  verdict,
  metrics,
}: {
  verdict: ReturnType<typeof computeAttemptVerdict>;
  metrics: ReturnType<typeof computeDecisionMetrics>;
}) {
  return (
    <section className={`rounded-lg border p-4 ${VERDICT_STYLES[verdict.kind]}`} aria-label="Verdict">
      <h2 className="text-xs font-medium uppercase tracking-wide opacity-80">Verdict</h2>
      <p className="mt-1 text-lg font-semibold">{verdict.label}</p>
      <p className="mt-1 text-sm opacity-90">{verdict.description}</p>
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        {metrics.map((metric) => (
          <ExperimentMetricExplainer key={metric.key} metric={metric} />
        ))}
      </div>
    </section>
  );
}

export function ExperimentDetail({ experimentId }: { experimentId: number }) {
  const { detail, loading, notFound, error } = useExperimentDetail(experimentId);

  if (loading && !detail) return <p className="text-sm text-gray-600 dark:text-gray-500">Loading...</p>;
  if (notFound) return <p className="text-sm text-gray-600 dark:text-gray-500">Experiment #{experimentId} not found.</p>;
  if (error) return <p className="text-sm text-red-700 dark:text-red-400">Failed to load experiment: {error}</p>;
  if (!detail) return null;

  const verdict = computeAttemptVerdict(detail);
  const decisionMetrics = computeDecisionMetrics(detail.comparison);

  return (
    <div className="space-y-6">
      <div>
        <Link href={`/feedback/experiment-runs/${detail.experiment_run.id}`} className="mb-2 inline-block text-sm text-blue-600 dark:text-blue-400">
          ← Parent run
        </Link>
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">{detail.experiment_run.name}</h1>
          <span className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${STATUS_COLORS[detail.status]}`}>
            attempt: {detail.status}
          </span>
          <span className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${RUN_STATUS_COLORS[detail.experiment_run.status]}`}>
            run: {detail.experiment_run.status}
          </span>
        </div>
        <p className="mt-1 text-xs text-gray-600 dark:text-gray-500">
          Experiment #{detail.id} (attempt {detail.attempt_number} of run #{detail.experiment_run.id}) ·
          Created {fmt(detail.created_at)} · Completed {fmt(detail.completed_at)}
          {detail.supersedes_experiment_id !== null && (
            <>
              {" "}
              · retry of{" "}
              <Link
                href={`/feedback/experiments/${detail.supersedes_experiment_id}`}
                className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
              >
                #{detail.supersedes_experiment_id}
              </Link>
            </>
          )}
        </p>
      </div>

      <VerdictHeader verdict={verdict} metrics={decisionMetrics} />

      {detail.reply_evidence && <ReplyComparisonViewer evidence={detail.reply_evidence} />}

      <details className="rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900 p-4">
        <SectionSummary>Baseline</SectionSummary>
        <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
          <DetailField label="Phase" value={detail.baseline.phase} />
          <DetailField
            label="Phase run"
            value={
              <Link href={detail.baseline.phase_run_url} className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300">
                #{detail.baseline.phase_run_id}
              </Link>
            }
          />
          <DetailField
            label="Trace"
            value={
              <Link href={detail.baseline.trace_url} className="font-mono text-[11px] text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300">
                {detail.baseline.trace_id}
              </Link>
            }
          />
          <DetailField label="Model" value={detail.baseline.model} />
          <DetailField
            label="Linked evaluation"
            value={detail.evaluation_id === null ? "— (not linked)" : `#${detail.evaluation_id}`}
          />
        </dl>
        <PromptDisclosure
          label="System prompt"
          prompt={detail.baseline.system_prompt}
          sha256={detail.baseline.system_prompt_sha256}
        />
      </details>

      <details className="rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900 p-4">
        <SectionSummary>Feedback snapshot &amp; policy</SectionSummary>
        <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
          <DetailField
            label="Snapshot"
            value={
              <Link href={detail.snapshot.snapshot_url} className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300">
                #{detail.snapshot.snapshot_id}
              </Link>
            }
          />
          <DetailField label="Snapshot phase" value={`#${detail.snapshot.snapshot_phase_id}`} />
          <DetailField label="Policy version" value={detail.snapshot.policy_version} />
          <DetailField label="Lookback days" value={detail.snapshot.lookback_days} />
          <DetailField label="Max grades" value={detail.snapshot.max_grades} />
        </dl>
      </details>

      <details className="rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900 p-4">
        <SectionSummary>Candidate</SectionSummary>
        <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
          <DetailField
            label="Trace"
            value={
              detail.candidate.trace_id === null || detail.candidate.trace_url === null ? (
                <Unavailable />
              ) : (
                <Link href={detail.candidate.trace_url} className="font-mono text-[11px] text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300">
                  {detail.candidate.trace_id}
                </Link>
              )
            }
          />
          <DetailField label="Model" value={detail.candidate.model} />
          <DetailField
            label="Grader attached"
            value={detail.experiment_run.candidate_config.grader_attached ? "yes" : "no"}
          />
          <DetailField
            label="Actual LLM calls"
            value={detail.candidate.llm_call_count ?? <Unavailable />}
          />
          <DetailField
            label="Actual cost"
            value={detail.candidate.cost === null ? <Unavailable /> : `$${detail.candidate.cost.toFixed(4)}`}
          />
        </dl>
        <PromptDisclosure
          label="System prompt"
          prompt={detail.candidate.system_prompt}
          sha256={detail.candidate.system_prompt_sha256}
        />
      </details>

      <BaselineEvidenceSection evidence={detail.baseline_evidence} />

      <ComparisonSection comparison={detail.comparison} status={detail.status} />
    </div>
  );
}
