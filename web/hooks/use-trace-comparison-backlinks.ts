"use client";

import { useEffect, useReducer } from "react";
import type { TraceComparisonBacklink } from "@/types/traces";

interface State {
  backlinks: TraceComparisonBacklink[];
  loading: boolean;
}

type Action = { type: "fetch" } | { type: "success"; backlinks: TraceComparisonBacklink[] };

function reducer(_state: State, action: Action): State {
  switch (action.type) {
    case "fetch":
      return { backlinks: [], loading: true };
    case "success":
      return { backlinks: action.backlinks, loading: false };
  }
}

// Resolves this trace's replay-comparison backlinks — zero or more,
// independent of the single evaluation_phase_runs backlink resolved by
// useTracePhaseRunBacklink.
export function useTraceComparisonBacklinks(traceId: string) {
  const [state, dispatch] = useReducer(reducer, { backlinks: [], loading: true });

  useEffect(() => {
    dispatch({ type: "fetch" });
    let canceled = false;

    if (traceId === "") {
      dispatch({ type: "success", backlinks: [] });
      return;
    }

    fetch(`/api/traces/${traceId}/comparisons`)
      .then((res) => {
        if (!res.ok) return [];
        return res.json() as Promise<TraceComparisonBacklink[]>;
      })
      .then((backlinks) => {
        if (!canceled) dispatch({ type: "success", backlinks });
      })
      .catch(() => {
        if (!canceled) dispatch({ type: "success", backlinks: [] });
      });

    return () => {
      canceled = true;
    };
  }, [traceId]);

  return state;
}
