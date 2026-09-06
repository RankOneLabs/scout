"use client";
import { useCallback, useEffect, useState } from "react";
import type { QueueDetail, QueueSummary } from "@/types/review-queues";

async function loadJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    const error = await response.json() as { detail?: string };
    throw new Error(error.detail ?? `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function useReviewQueues() {
  const [queues, setQueues] = useState<QueueSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    loadJson<QueueSummary[]>("/api/grading/queues").then((data) => {
      if (active) setQueues(data);
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "Cannot load queues");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);
  return { queues, error, loading };
}

export function useReviewQueue(digest: string) {
  const [detail, setDetail] = useState<QueueDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    const data = await loadJson<QueueDetail>(`/api/grading/queues/${digest}`);
    setDetail(data);
    setError(null);
  }, [digest]);
  useEffect(() => {
    let active = true;
    loadJson<QueueDetail>(`/api/grading/queues/${digest}`).then((data) => {
      if (active) setDetail(data);
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "Cannot load queue");
    });
    return () => { active = false; };
  }, [digest]);
  return { detail, error, refresh };
}
