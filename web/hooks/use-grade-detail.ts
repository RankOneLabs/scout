"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { GradeDetailResponse } from "@/types/feedback-grades";

async function fetchDetail(
  gradeId: number,
  params: Record<string, string> = {}
): Promise<GradeDetailResponse> {
  const search = new URLSearchParams(params);
  const qs = search.toString();
  const res = await fetch(`/api/feedback/grades/${gradeId}${qs ? `?${qs}` : ""}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<GradeDetailResponse>;
}

// Loads the grade detail once, then extends its two paginated histories
// (revisions, snapshot use) independently via their own opaque cursors —
// mirrors the API's own per-history pagination.
export function useGradeDetail(gradeId: number | null) {
  const [detail, setDetail] = useState<GradeDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);

  const mainFetchId = useRef(0);
  const revisionFetchId = useRef(0);
  const snapshotUseFetchId = useRef(0);
  const phaseRunFetchId = useRef(0);

  useEffect(() => {
    if (gradeId === null) {
      // Invalidate any in-flight fetch for the previous gradeId so its
      // response can't land after navigating away and overwrite state
      // with stale data.
      ++mainFetchId.current;
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setDetail(null);
      setLoading(false);
      setError(null);
      setNotFound(false);
      return;
    }
    const id = ++mainFetchId.current;
    setLoading(true);
    setError(null);
    setNotFound(false);
    fetchDetail(gradeId)
      .then((resp) => {
        if (id !== mainFetchId.current) return;
        setDetail(resp);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (id !== mainFetchId.current) return;
        if (err instanceof Error && err.message === "HTTP 404") {
          setNotFound(true);
        } else {
          setError(err instanceof Error ? err.message : String(err));
        }
        setLoading(false);
      });
  }, [gradeId]);

  const loadMoreRevisions = useCallback(() => {
    if (gradeId === null || !detail?.revision_history.has_more || !detail.revision_history.next_cursor) {
      return;
    }
    const id = ++revisionFetchId.current;
    fetchDetail(gradeId, { revisionCursor: detail.revision_history.next_cursor })
      .then((resp) => {
        if (id !== revisionFetchId.current) return;
        setDetail((current) =>
          current === null
            ? current
            : {
                ...current,
                revision_history: {
                  data: [...current.revision_history.data, ...resp.revision_history.data],
                  has_more: resp.revision_history.has_more,
                  next_cursor: resp.revision_history.next_cursor,
                },
              }
        );
      })
      .catch((err: unknown) => {
        if (id !== revisionFetchId.current) return;
        setError(err instanceof Error ? err.message : String(err));
      });
  }, [gradeId, detail]);

  const loadMoreSnapshotUseHistory = useCallback(() => {
    if (
      gradeId === null ||
      !detail?.snapshot_use_history.has_more ||
      !detail.snapshot_use_history.next_cursor
    ) {
      return;
    }
    const id = ++snapshotUseFetchId.current;
    fetchDetail(gradeId, { snapshotUseCursor: detail.snapshot_use_history.next_cursor })
      .then((resp) => {
        if (id !== snapshotUseFetchId.current) return;
        setDetail((current) =>
          current === null
            ? current
            : {
                ...current,
                snapshot_use_history: {
                  data: [...current.snapshot_use_history.data, ...resp.snapshot_use_history.data],
                  has_more: resp.snapshot_use_history.has_more,
                  next_cursor: resp.snapshot_use_history.next_cursor,
                },
              }
        );
      })
      .catch((err: unknown) => {
        if (id !== snapshotUseFetchId.current) return;
        setError(err instanceof Error ? err.message : String(err));
      });
  }, [gradeId, detail]);

  const loadMorePhaseRuns = useCallback(() => {
    if (gradeId === null || !detail?.phase_runs.has_more || !detail.phase_runs.next_cursor) {
      return;
    }
    const id = ++phaseRunFetchId.current;
    fetchDetail(gradeId, { phaseRunCursor: detail.phase_runs.next_cursor })
      .then((resp) => {
        if (id !== phaseRunFetchId.current) return;
        setDetail((current) =>
          current === null
            ? current
            : {
                ...current,
                phase_runs: {
                  data: [...current.phase_runs.data, ...resp.phase_runs.data],
                  has_more: resp.phase_runs.has_more,
                  next_cursor: resp.phase_runs.next_cursor,
                },
              }
        );
      })
      .catch((err: unknown) => {
        if (id !== phaseRunFetchId.current) return;
        setError(err instanceof Error ? err.message : String(err));
      });
  }, [gradeId, detail]);

  const refetch = useCallback(() => {
    if (gradeId === null) return;
    const id = ++mainFetchId.current;
    fetchDetail(gradeId).then((resp) => {
      if (id !== mainFetchId.current) return;
      setDetail(resp);
    });
  }, [gradeId]);

  return {
    detail,
    loading,
    error,
    notFound,
    loadMoreRevisions,
    loadMoreSnapshotUseHistory,
    loadMorePhaseRuns,
    refetch,
  };
}
