"use client";

import Link from "next/link";
import type { TraceSpan } from "@/types/schema";
import {
  buildSpanTree,
  formatTimestamp,
  formatDurationMs,
  type SpanTreeNode,
} from "@/lib/transforms";
import { SpanRow } from "@/components/molecules/SpanRow";
import { DetailField } from "@/components/atoms/DetailField";
import { useTracePhaseRunBacklink } from "@/hooks/use-trace-phase-run-backlink";
import { useTraceComparisonBacklinks } from "@/hooks/use-trace-comparison-backlinks";

interface TraceDetailProps {
  spans: TraceSpan[];
}

function RenderNodes({ nodes, depth }: { nodes: SpanTreeNode[]; depth: number }) {
  return (
    <>
      {nodes.map((node) => (
        <SpanRow key={node.span.id} span={node.span} depth={depth}>
          {node.children.length > 0 && (
            <RenderNodes nodes={node.children} depth={depth + 1} />
          )}
        </SpanRow>
      ))}
    </>
  );
}

export function TraceDetail({ spans }: TraceDetailProps) {
  const tree = buildSpanTree(spans);
  const root = spans.find((s) => s.parent_id === null);
  const { backlink, loading: phaseRunLoading } = useTracePhaseRunBacklink(root?.trace_id ?? "");
  const { backlinks: comparisonBacklinks, loading: comparisonsLoading } = useTraceComparisonBacklinks(
    root?.trace_id ?? ""
  );

  return (
    <div className="space-y-4">
      {root && (
        <div className="rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 p-4">
          <div className="grid grid-cols-1 gap-4 text-sm sm:grid-cols-3">
            <DetailField as="span" label="Trace ID" value={root.trace_id} valueClassName="font-mono text-xs text-gray-700 dark:text-gray-300" />
            <DetailField as="span" label="Started" value={formatTimestamp(root.started_at)} valueClassName="text-gray-700 dark:text-gray-300" />
            <DetailField as="span" label="Duration" value={formatDurationMs(root.duration_ms)} valueClassName="text-gray-700 dark:text-gray-300" />
          </div>
        </div>
      )}

      {!phaseRunLoading && (
        <div className="rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 p-4">
          <div className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">
            Linked phase run
          </div>
          {backlink === null ? (
            <p className="text-xs italic text-gray-600 dark:text-gray-500">
              No linked phase run for this trace — only a stored evaluation_phase_runs.trace_id
              match counts, never inferred from timestamps, prompts, models, or post identity.
            </p>
          ) : (
            <div className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
              <DetailField as="span" label="Phase" value={backlink.phase} valueClassName="text-gray-700 dark:text-gray-300" />
              <DetailField
                as="span"
                label="Phase run"
                value={
                  <Link href={`/feedback/phase-runs/${backlink.id}`} className="text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300">
                    #{backlink.id}
                  </Link>
                }
              />
              <DetailField
                as="span"
                label="Snapshot phase"
                value={
                  <Link
                    href={`/feedback?snapshotId=${backlink.snapshot_id}#phase-${backlink.phase}`}
                    className="text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300"
                  >
                    #{backlink.snapshot_phase_id}
                  </Link>
                }
                valueClassName="text-gray-700 dark:text-gray-300"
              />
              <DetailField
                as="span"
                label="Evaluation"
                value={
                  backlink.evaluation_id === null ? (
                    "— (not linked)"
                  ) : (
                    <>
                      <Link
                        href={`/scans/${backlink.scan_id}#evaluation-${backlink.evaluation_id}`}
                        className="text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300"
                      >
                        #{backlink.evaluation_id}
                      </Link>
                      {backlink.grade_id !== null && (
                        <>
                          {" · "}
                          <Link
                            href={`/feedback/grades/${backlink.grade_id}`}
                            className="text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300"
                          >
                            grade #{backlink.grade_id}
                          </Link>
                        </>
                      )}
                    </>
                  )
                }
                valueClassName="text-gray-700 dark:text-gray-300"
              />
            </div>
          )}
        </div>
      )}

      {!comparisonsLoading && (
        <div className="rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 p-4">
          <div className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">
            Replay comparisons{comparisonBacklinks.length > 0 && ` (${comparisonBacklinks.length})`}
          </div>
          {comparisonBacklinks.length === 0 ? (
            <p className="text-xs italic text-gray-600 dark:text-gray-500">
              No replay comparisons for this trace — only a stored trace_diff trace_a_id/
              trace_b_id match counts, never inferred from timestamps, prompts, models, or post
              identity.
            </p>
          ) : (
            <ul className="space-y-1">
              {comparisonBacklinks.map((link) => (
                <li key={link.comparison_id} className="text-sm">
                  <Link href={link.experiment_url} className="text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300">
                    {link.experiment_name}
                  </Link>
                  <span className="ml-2 text-gray-600 dark:text-gray-500">
                    ({link.role}, {link.experiment_status})
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="rounded-lg border border-gray-200 dark:border-gray-800">
        <RenderNodes nodes={tree} depth={0} />
      </div>
    </div>
  );
}
