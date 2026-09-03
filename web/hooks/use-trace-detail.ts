"use client";

import { useEffect, useReducer } from "react";
import type { TraceSpan } from "@/types/schema";

interface State {
  spans: TraceSpan[];
  loading: boolean;
}

type Action =
  | { type: "fetch" }
  | { type: "success"; spans: TraceSpan[] }
  | { type: "error" };

function reducer(_state: State, action: Action): State {
  switch (action.type) {
    case "fetch":
      return { spans: [], loading: true };
    case "success":
      return { spans: action.spans, loading: false };
    case "error":
      return { spans: [], loading: false };
  }
}

export function useTraceDetail(traceId: string) {
  const [state, dispatch] = useReducer(reducer, {
    spans: [],
    loading: true,
  });

  useEffect(() => {
    dispatch({ type: "fetch" });

    let canceled = false;

    fetch(`/api/traces/${traceId}`)
      .then((res) => {
        if (!res.ok) return [];
        return res.json();
      })
      .then((data) => {
        if (!canceled) dispatch({ type: "success", spans: data });
      })
      .catch(() => {
        if (!canceled) dispatch({ type: "error" });
      });

    return () => {
      canceled = true;
    };
  }, [traceId]);

  return state;
}
