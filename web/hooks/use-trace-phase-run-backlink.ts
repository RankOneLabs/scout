"use client";

import { useEffect, useReducer } from "react";
import type { PhaseRunDetail } from "@/types/feedback-grades";

interface State {
  backlink: PhaseRunDetail | null;
  loading: boolean;
}

type Action = { type: "fetch" } | { type: "success"; backlink: PhaseRunDetail | null };

function reducer(_state: State, action: Action): State {
  switch (action.type) {
    case "fetch":
      return { backlink: null, loading: true };
    case "success":
      return { backlink: action.backlink, loading: false };
  }
}

// Resolves this trace's evaluation_phase_runs backlink, if any — historical
// traces (predating phase-run linkage) and non-phase traces (e.g. a
// PIPELINE_RUN root) simply have none.
export function useTracePhaseRunBacklink(traceId: string) {
  const [state, dispatch] = useReducer(reducer, { backlink: null, loading: true });

  useEffect(() => {
    dispatch({ type: "fetch" });
    let canceled = false;

    // traceId is "" before the trace's root span has loaded (TraceDetail
    // passes root?.trace_id ?? "") — skip the request rather than hitting
    // /api/traces//phase-run.
    if (traceId === "") {
      dispatch({ type: "success", backlink: null });
      return;
    }

    fetch(`/api/traces/${traceId}/phase-run`)
      .then((res) => {
        if (!res.ok) return null;
        return res.json() as Promise<PhaseRunDetail>;
      })
      .then((backlink) => {
        if (!canceled) dispatch({ type: "success", backlink });
      })
      .catch(() => {
        if (!canceled) dispatch({ type: "success", backlink: null });
      });

    return () => {
      canceled = true;
    };
  }, [traceId]);

  return state;
}
