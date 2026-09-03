"use client";

import { useEffect, useReducer } from "react";
import type { ExperimentDetailResponse } from "@/types/feedback-experiments";

interface State {
  detail: ExperimentDetailResponse | null;
  loading: boolean;
  notFound: boolean;
  error: string | null;
}

type Action =
  | { type: "fetch" }
  | { type: "success"; detail: ExperimentDetailResponse }
  | { type: "not-found" }
  | { type: "error"; error: string };

function reducer(_state: State, action: Action): State {
  switch (action.type) {
    case "fetch":
      return { detail: null, loading: true, notFound: false, error: null };
    case "success":
      return { detail: action.detail, loading: false, notFound: false, error: null };
    case "not-found":
      return { detail: null, loading: false, notFound: true, error: null };
    case "error":
      return { detail: null, loading: false, notFound: false, error: action.error };
  }
}

export function useExperimentDetail(experimentId: number) {
  const [state, dispatch] = useReducer(reducer, {
    detail: null,
    loading: true,
    notFound: false,
    error: null,
  });

  useEffect(() => {
    dispatch({ type: "fetch" });

    let canceled = false;

    fetch(`/api/feedback/experiments/${experimentId}`)
      .then((res) => {
        if (res.status === 404) return null;
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<ExperimentDetailResponse>;
      })
      .then((detail) => {
        if (canceled) return;
        if (detail === null) {
          dispatch({ type: "not-found" });
        } else {
          dispatch({ type: "success", detail });
        }
      })
      .catch((err: unknown) => {
        if (!canceled) {
          dispatch({ type: "error", error: err instanceof Error ? err.message : String(err) });
        }
      });

    return () => {
      canceled = true;
    };
  }, [experimentId]);

  return state;
}
