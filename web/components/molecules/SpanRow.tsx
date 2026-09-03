"use client";

import { useState } from "react";
import type { TraceSpan } from "@/types/schema";
import { SPAN_KIND_COLORS, SPAN_STATUS_COLORS } from "@/lib/design-tokens";
import { getSpanStatus, formatDurationMs } from "@/lib/transforms";

interface SpanRowProps {
  span: TraceSpan;
  depth: number;
  children?: React.ReactNode;
}

export function SpanRow({ span, depth, children }: SpanRowProps) {
  const [expanded, setExpanded] = useState(false);
  const status = getSpanStatus(span);
  const kindColor =
    SPAN_KIND_COLORS[span.kind] ??
    "bg-gray-100 text-gray-700 border-gray-300 dark:bg-gray-500/20 dark:text-gray-400 dark:border-gray-500/30";
  const statusColor = SPAN_STATUS_COLORS[status];

  return (
    <div>
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="flex w-full flex-wrap items-center gap-1.5 rounded px-2 py-1.5 text-left text-sm transition-colors hover:bg-gray-100 dark:hover:bg-gray-800/50 sm:gap-2"
        style={{ paddingLeft: `${depth * 20 + 8}px` }}
      >
        <span className="text-xs text-gray-600 dark:text-gray-500">{expanded ? "▼" : "▶"}</span>
        <span className="font-medium text-gray-800 dark:text-gray-200">{span.name}</span>
        <span
          className={`rounded border px-1.5 py-0.5 text-xs ${kindColor}`}
        >
          {span.kind}
        </span>
        <span
          className={`rounded border px-1.5 py-0.5 text-xs ${statusColor}`}
        >
          {status}
        </span>
        <span className="ml-auto text-xs text-gray-600 dark:text-gray-500">
          {formatDurationMs(span.duration_ms)}
        </span>
      </button>

      {expanded && (
        <div
          className="mb-1 space-y-1 rounded bg-gray-50 dark:bg-gray-900 p-2 text-xs"
          style={{ marginLeft: `${depth * 20 + 28}px` }}
        >
          {span.error && (
            <div>
              <span className="font-medium text-red-600 dark:text-red-400">Error: </span>
              <pre className="mt-1 overflow-x-auto whitespace-pre-wrap text-red-700 dark:text-red-300">
                {span.error}
              </pre>
            </div>
          )}
          {span.input !== null && (
            <div>
              <span className="font-medium text-gray-600 dark:text-gray-400">Input:</span>
              <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap text-gray-700 dark:text-gray-300">
                {typeof span.input === "string"
                  ? span.input
                  : JSON.stringify(span.input, null, 2)}
              </pre>
            </div>
          )}
          {span.output !== null && (
            <div>
              <span className="font-medium text-gray-600 dark:text-gray-400">Output:</span>
              <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap text-gray-700 dark:text-gray-300">
                {typeof span.output === "string"
                  ? span.output
                  : JSON.stringify(span.output, null, 2)}
              </pre>
            </div>
          )}
          {span.usage_input_tokens !== null &&
            span.usage_output_tokens !== null && (
              <div className="text-gray-600 dark:text-gray-500">
                Tokens: {span.usage_input_tokens} in /{" "}
                {span.usage_output_tokens} out
                {span.usage_cost !== null &&
                  ` ($${span.usage_cost.toFixed(4)})`}
              </div>
            )}
        </div>
      )}

      {children}
    </div>
  );
}
