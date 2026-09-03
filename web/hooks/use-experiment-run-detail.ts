"use client";

import { useEffect, useState } from "react";
import type { ExperimentRunDetailResponse } from "@/types/feedback-experiments";

type DetailState = {
  id: number;
  detail: ExperimentRunDetailResponse | null;
  loading: boolean;
  error: string | null;
};

export function useExperimentRunDetail(id: number) {
  const [state, setState] = useState<DetailState>({ id, detail: null, loading: true, error: null });
  useEffect(() => {
    let active = true;
    fetch(`/api/feedback/experiment-runs/${id}`).then((response) => {
      if (response.status === 404) throw new Error("Experiment run not found");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json() as Promise<ExperimentRunDetailResponse>;
    }).then((value) => { if (active) setState({ id, detail: value, loading: false, error: null }); })
      .catch((reason: unknown) => { if (active) setState({ id, detail: null, loading: false, error: reason instanceof Error ? reason.message : String(reason) }); });
    return () => { active = false; };
  }, [id]);
  return state.id === id ? state : { id, detail: null, loading: true, error: null };
}
