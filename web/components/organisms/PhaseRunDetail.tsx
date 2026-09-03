"use client";

import Link from "next/link";
import { usePhaseRunDetail } from "@/hooks/use-phase-run-detail";
import { DetailField } from "@/components/atoms/DetailField";
import { parseUtc } from "@/lib/transforms";

function fmt(iso: string): string {
  const d = parseUtc(iso);
  return Number.isNaN(d.valueOf()) ? iso : d.toLocaleString();
}

// Read-only stored-key identity for one relevance/reply_draft/critic phase
// attempt — every field here is a link column on evaluation_phase_runs,
// never prompt content or grade evidence.
export function PhaseRunDetail({ phaseRunId }: { phaseRunId: number }) {
  const { detail, loading, notFound } = usePhaseRunDetail(phaseRunId);

  if (loading) return <p className="text-sm text-gray-600 dark:text-gray-500">Loading...</p>;
  if (notFound) return <p className="text-sm text-gray-600 dark:text-gray-500">Phase run #{phaseRunId} not found.</p>;
  if (!detail) return <p className="text-sm text-red-600 dark:text-red-400">Failed to load phase run.</p>;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
          Phase run #{detail.id} — {detail.phase}
        </h1>
        <p className="mt-1 text-xs text-gray-600 dark:text-gray-500">
          <Link href={`/scans/${detail.scan_id}`} className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300">
            Scan #{detail.scan_id}
          </Link>{" "}
          · Post #{detail.post_id}
          {detail.evaluation_id !== null && (
            <>
              {" · "}
              <Link
                href={`/scans/${detail.scan_id}#evaluation-${detail.evaluation_id}`}
                className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
              >
                Evaluation #{detail.evaluation_id}
              </Link>
            </>
          )}
        </p>
      </div>

      <section className="rounded-lg border border-gray-200 bg-gray-50 p-4 dark:border-gray-800 dark:bg-gray-900">
        <h2 className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">Stored identity</h2>
        <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-3">
          <DetailField label="Status" value={detail.status} />
          <DetailField label="Model" value={detail.model} />
          <DetailField
            label="Snapshot phase"
            value={
              <Link
                href={`/feedback?snapshotId=${detail.snapshot_id}#phase-${detail.phase}`}
                className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
              >
                #{detail.snapshot_phase_id}
              </Link>
            }
          />
          <DetailField label="Created" value={fmt(detail.created_at)} />
          <DetailField
            label="Evaluation"
            value={
              detail.evaluation_id === null ? (
                "— (not linked)"
              ) : (
                <Link
                  href={`/scans/${detail.scan_id}#evaluation-${detail.evaluation_id}`}
                  className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
                >
                  #{detail.evaluation_id}
                </Link>
              )
            }
          />
          <DetailField
            label="Grade"
            value={
              detail.grade_id === null ? (
                "— (not graded)"
              ) : (
                <Link
                  href={`/feedback/grades/${detail.grade_id}`}
                  className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
                >
                  #{detail.grade_id}
                </Link>
              )
            }
          />
          <DetailField
            label="Jig trace"
            value={
              <Link
                href={`/traces/${detail.trace_id}`}
                className="font-mono text-[11px] text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
              >
                {detail.trace_id}
              </Link>
            }
          />
        </dl>
      </section>
    </div>
  );
}
