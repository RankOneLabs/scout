"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useTraceDetail } from "@/hooks/use-trace-detail";
import { TraceDetail } from "@/components/organisms/TraceDetail";

export default function TraceDetailPage() {
  const params = useParams<{ id: string }>();
  const { spans, loading } = useTraceDetail(params.id);

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">Loading...</p>
      </div>
    );
  }

  if (spans.length === 0) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">Trace not found.</p>
      </div>
    );
  }

  const rootName = spans.find((s) => s.parent_id === null)?.name ?? "Trace";

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-2 sm:gap-3">
        <Link
          href="/traces"
          className="text-sm text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200"
        >
          Traces
        </Link>
        <span className="text-gray-500 dark:text-gray-600">/</span>
        <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100 sm:text-2xl">{rootName}</h1>
      </div>
      <TraceDetail spans={spans} />
    </div>
  );
}
