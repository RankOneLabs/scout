"use client";

import { useEffect, useReducer } from "react";
import type { PhaseRunDetail } from "@/types/feedback-grades";

interface State {
  detail: PhaseRunDetail | null;
  loading: boolean;
  notFound: boolean;
}

type Action =
  | { type: "fetch" }
  | { type: "success"; detail: PhaseRunDetail }
  | { type: "not-found" }
  | { type: "error" };

function reducer(_state: State, action: Action): State {
  switch (action.type) {
    case "fetch":
      return { detail: null, loading: true, notFound: false };
    case "success":
      return { detail: action.detail, loading: false, notFound: false };
    case "not-found":
      return { detail: null, loading: false, notFound: true };
    case "error":
      return { detail: null, loading: false, notFound: false };
  }
}

export function usePhaseRunDetail(phaseRunId: number) {
  const [state, dispatch] = useReducer(reducer, {
    detail: null,
    loading: true,
    notFound: false,
  });

  useEffect(() => {
    dispatch({ type: "fetch" });

    let canceled = false;

    fetch(`/api/feedback/phase-runs/${phaseRunId}`)
      .then((res) => {
        if (res.status === 404) return null;
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<PhaseRunDetail>;
      })
      .then((detail) => {
        if (canceled) return;
        if (detail === null) {
          dispatch({ type: "not-found" });
        } else {
          dispatch({ type: "success", detail });
        }
      })
      .catch(() => {
        if (!canceled) dispatch({ type: "error" });
      });

    return () => {
      canceled = true;
    };
  }, [phaseRunId]);

  return state;
}
