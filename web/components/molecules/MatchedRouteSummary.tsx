"use client";

import { Badge } from "@/components/atoms/Badge";
import type { MatchedRoute } from "@/types/schema";

interface MatchedRouteSummaryProps {
  route: MatchedRoute | null;
}

function formatContexts(values: string[]): string {
  return values.length > 0 ? values.join(", ") : "—";
}

export function MatchedRouteSummary({ route }: MatchedRouteSummaryProps) {
  if (!route) return null;

  const promptNames = [
    route.evaluate_prompt,
    route.respond_prompt,
    route.critique_prompt,
  ].filter((prompt): prompt is string => Boolean(prompt));

  return (
    <div className="mt-3 rounded-md border border-sky-300 dark:border-sky-500/20 bg-sky-50 dark:bg-sky-500/5 p-3 text-xs text-sky-900 dark:text-sky-100/90">
      <div className="flex flex-wrap items-center gap-2">
        <span className="uppercase tracking-[0.18em] text-sky-700 dark:text-sky-300/80">
          Matched route
        </span>
        <Badge label={route.project_key} variant="project" />
        <span className="rounded-full border border-sky-300 dark:border-sky-500/30 bg-sky-100 dark:bg-sky-500/10 px-2 py-0.5 text-[11px] font-medium text-sky-700 dark:text-sky-200">
          {route.keyword}
        </span>
        <span className="rounded-full border border-sky-300 dark:border-sky-500/20 bg-sky-100 dark:bg-sky-500/10 px-2 py-0.5 text-[11px] font-medium text-sky-700 dark:text-sky-200">
          {route.match_type}
        </span>
      </div>

      <div className="mt-2 space-y-1 text-sky-900 dark:text-sky-100/80">
        <p>
          <span className="text-sky-700 dark:text-sky-300/80">Intent:</span>{" "}
          {route.intent ?? "—"}
        </p>
        <p>
          <span className="text-sky-700 dark:text-sky-300/80">Positive context:</span>{" "}
          {formatContexts(route.positive_context)}
        </p>
        <p>
          <span className="text-sky-700 dark:text-sky-300/80">Negative context:</span>{" "}
          {formatContexts(route.negative_context)}
        </p>
        {promptNames.length > 0 && (
          <p>
            <span className="text-sky-700 dark:text-sky-300/80">Prompt overrides:</span>{" "}
            {promptNames.join(", ")}
          </p>
        )}
      </div>

      {route.resolved_prompt_bundle && (
        <details className="mt-3 rounded border border-sky-300 dark:border-sky-500/15 bg-sky-50 dark:bg-slate-950/60 p-2">
          <summary className="cursor-pointer text-[11px] uppercase tracking-[0.18em] text-sky-700 dark:text-sky-300/80">
            Resolved prompt bundle
          </summary>
          <div className="mt-2 space-y-2">
            {route.resolved_prompt_bundle.evaluate && (
              <div>
                <p className="text-[11px] uppercase tracking-[0.18em] text-sky-700 dark:text-sky-300/70">
                  Evaluate
                </p>
                <pre className="mt-1 whitespace-pre-wrap break-words text-[11px] text-sky-900 dark:text-sky-100/80">
                  {route.resolved_prompt_bundle.evaluate}
                </pre>
              </div>
            )}
            {route.resolved_prompt_bundle.respond && (
              <div>
                <p className="text-[11px] uppercase tracking-[0.18em] text-sky-700 dark:text-sky-300/70">
                  Respond
                </p>
                <pre className="mt-1 whitespace-pre-wrap break-words text-[11px] text-sky-900 dark:text-sky-100/80">
                  {route.resolved_prompt_bundle.respond}
                </pre>
              </div>
            )}
            {route.resolved_prompt_bundle.critique && (
              <div>
                <p className="text-[11px] uppercase tracking-[0.18em] text-sky-700 dark:text-sky-300/70">
                  Critique
                </p>
                <pre className="mt-1 whitespace-pre-wrap break-words text-[11px] text-sky-900 dark:text-sky-100/80">
                  {route.resolved_prompt_bundle.critique}
                </pre>
              </div>
            )}
          </div>
        </details>
      )}
    </div>
  );
}
