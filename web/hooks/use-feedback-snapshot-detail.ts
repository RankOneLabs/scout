"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { FEEDBACK_PHASES } from "@/types/feedback";
import type {
  FeedbackGradeUse,
  FeedbackPhase,
  FeedbackPhaseRunConsumer,
  FeedbackPhaseSummary,
  FeedbackSnapshotHeader,
  FeedbackUseFilter,
} from "@/types/feedback";

interface PhaseItemsState {
  data: FeedbackGradeUse[];
  has_more: boolean;
  next_cursor: string | null;
  use_filter: FeedbackUseFilter;
}

interface PhaseConsumersState {
  data: FeedbackPhaseRunConsumer[];
  has_more: boolean;
  next_cursor: string | null;
}

interface DetailResponse {
  snapshot: FeedbackSnapshotHeader;
  phases: Record<FeedbackPhase, FeedbackPhaseSummary>;
  items: {
    phase: FeedbackPhase;
    use_filter: FeedbackUseFilter;
    data: FeedbackGradeUse[];
    has_more: boolean;
    next_cursor: string | null;
  };
  consumers: {
    phase: FeedbackPhase;
    data: FeedbackPhaseRunConsumer[];
    has_more: boolean;
    next_cursor: string | null;
  };
}

function emptyConsumersByPhase(): Record<FeedbackPhase, PhaseConsumersState> {
  return {
    relevance: { data: [], has_more: false, next_cursor: null },
    reply_draft: { data: [], has_more: false, next_cursor: null },
    critic: { data: [], has_more: false, next_cursor: null },
  };
}

function emptyItemsByPhase(): Record<FeedbackPhase, PhaseItemsState> {
  return {
    relevance: { data: [], has_more: false, next_cursor: null, use_filter: "all" },
    reply_draft: { data: [], has_more: false, next_cursor: null, use_filter: "all" },
    critic: { data: [], has_more: false, next_cursor: null, use_filter: "all" },
  };
}

function emptyPhaseFetchIds(): Record<FeedbackPhase, number> {
  return { relevance: 0, reply_draft: 0, critic: 0 };
}

async function fetchDetail(
  snapshotId: number,
  phase: FeedbackPhase,
  use: FeedbackUseFilter,
  cursors: { item?: string | null; consumer?: string | null } = {}
): Promise<DetailResponse> {
  const search = new URLSearchParams({ phase, use });
  if (cursors.item) search.set("itemCursor", cursors.item);
  if (cursors.consumer) search.set("consumerCursor", cursors.consumer);
  const res = await fetch(`/api/feedback/snapshots/${snapshotId}?${search.toString()}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<DetailResponse>;
}

// Loads the snapshot header and all three phase summaries once, then
// fetches each phase's grade-use and phase-consumer pages independently.
// Switching one phase's filter or advancing either cursor never mutates
// another phase's client-side page.
export function useFeedbackSnapshotDetail(snapshotId: number | null) {
  const [snapshot, setSnapshot] = useState<FeedbackSnapshotHeader | null>(null);
  const [phases, setPhases] = useState<Record<FeedbackPhase, FeedbackPhaseSummary> | null>(null);
  const [items, setItems] = useState<Record<FeedbackPhase, PhaseItemsState>>(emptyItemsByPhase);
  const [consumers, setConsumers] = useState<Record<FeedbackPhase, PhaseConsumersState>>(
    emptyConsumersByPhase
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);

  // Monotonic per-request tokens. mainFetchId guards the header/summary
  // load; phaseFetchId (shared by loadMore and setUseFilter) guards each
  // phase's item requests so a slower stale response — e.g. an "excluded"
  // filter click followed quickly by "included" — can never overwrite a
  // newer one once it lands.
  const mainFetchId = useRef(0);
  const phaseFetchId = useRef<Record<FeedbackPhase, number>>(emptyPhaseFetchIds());
  const consumerFetchId = useRef<Record<FeedbackPhase, number>>(emptyPhaseFetchIds());

  useEffect(() => {
    if (snapshotId === null) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setLoading(false);
      return;
    }
    const id = ++mainFetchId.current;
    FEEDBACK_PHASES.forEach((phase) => {
      phaseFetchId.current[phase] += 1;
      consumerFetchId.current[phase] += 1;
    });
    setLoading(true);
    setError(null);
    setNotFound(false);

    Promise.all(FEEDBACK_PHASES.map((phase) => fetchDetail(snapshotId, phase, "all")))
      .then((responses) => {
        if (id !== mainFetchId.current) return;
        setSnapshot(responses[0].snapshot);
        setPhases(responses[0].phases);
        const next = emptyItemsByPhase();
        const nextConsumers = emptyConsumersByPhase();
        FEEDBACK_PHASES.forEach((phase, i) => {
          const resp = responses[i].items;
          next[phase] = {
            data: resp.data,
            has_more: resp.has_more,
            next_cursor: resp.next_cursor,
            use_filter: resp.use_filter,
          };
          nextConsumers[phase] = {
            data: responses[i].consumers.data,
            has_more: responses[i].consumers.has_more,
            next_cursor: responses[i].consumers.next_cursor,
          };
        });
        setItems(next);
        setConsumers(nextConsumers);
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
  }, [snapshotId]);

  const loadMore = useCallback(
    (phase: FeedbackPhase) => {
      if (snapshotId === null) return;
      const state = items[phase];
      if (!state.has_more || !state.next_cursor) return;
      const id = ++phaseFetchId.current[phase];
      fetchDetail(snapshotId, phase, state.use_filter, { item: state.next_cursor })
        .then((resp) => {
          if (id !== phaseFetchId.current[phase]) return;
          setItems((current) => ({
            ...current,
            [phase]: {
              data: [...current[phase].data, ...resp.items.data],
              has_more: resp.items.has_more,
              next_cursor: resp.items.next_cursor,
              use_filter: resp.items.use_filter,
            },
          }));
        })
        .catch((err: unknown) => {
          if (id !== phaseFetchId.current[phase]) return;
          setError(err instanceof Error ? err.message : String(err));
        });
    },
    [snapshotId, items]
  );

  const loadMoreConsumers = useCallback(
    (phase: FeedbackPhase) => {
      if (snapshotId === null) return;
      const state = consumers[phase];
      if (!state.has_more || !state.next_cursor) return;
      const id = ++consumerFetchId.current[phase];
      fetchDetail(snapshotId, phase, items[phase].use_filter, { consumer: state.next_cursor })
        .then((resp) => {
          if (id !== consumerFetchId.current[phase]) return;
          setConsumers((current) => ({
            ...current,
            [phase]: {
              data: [...current[phase].data, ...resp.consumers.data],
              has_more: resp.consumers.has_more,
              next_cursor: resp.consumers.next_cursor,
            },
          }));
        })
        .catch((err: unknown) => {
          if (id !== consumerFetchId.current[phase]) return;
          setError(err instanceof Error ? err.message : String(err));
        });
    },
    [snapshotId, consumers, items]
  );

  const setUseFilter = useCallback(
    (phase: FeedbackPhase, use: FeedbackUseFilter) => {
      if (snapshotId === null) return;
      const id = ++phaseFetchId.current[phase];
      fetchDetail(snapshotId, phase, use)
        .then((resp) => {
          if (id !== phaseFetchId.current[phase]) return;
          setItems((current) => ({
            ...current,
            [phase]: {
              data: resp.items.data,
              has_more: resp.items.has_more,
              next_cursor: resp.items.next_cursor,
              use_filter: resp.items.use_filter,
            },
          }));
        })
        .catch((err: unknown) => {
          if (id !== phaseFetchId.current[phase]) return;
          setError(err instanceof Error ? err.message : String(err));
        });
    },
    [snapshotId]
  );

  return {
    snapshot,
    phases,
    items,
    consumers,
    loading,
    error,
    notFound,
    loadMore,
    loadMoreConsumers,
    setUseFilter,
  };
}
