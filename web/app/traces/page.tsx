"use client";

import { useTraces } from "@/hooks/use-traces";
import { TraceList } from "@/components/organisms/TraceList";

export default function TracesPage() {
  const { traces, loading } = useTraces();

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">Loading...</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Traces</h1>
      <TraceList traces={traces} />
    </div>
  );
}
