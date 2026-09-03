"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ProjectKeywordRow } from "@/types/schema";

interface UseSettingsKeywordsState {
  keywords: ProjectKeywordRow[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useSettingsKeywords(projectKey?: string): UseSettingsKeywordsState {
  const [keywords, setKeywords] = useState<ProjectKeywordRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fetchId = useRef(0);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const id = ++fetchId.current;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    const url = projectKey
      ? `/api/settings/keywords?project_key=${encodeURIComponent(projectKey)}&include_inactive=true`
      : "/api/settings/keywords?include_inactive=true";
    fetch(url)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return (await res.json()) as { keywords: ProjectKeywordRow[] };
      })
      .then((body) => {
        if (id !== fetchId.current) return;
        setKeywords(body.keywords ?? []);
        setError(null);
        setLoading(false);
      })
      .catch((err: Error) => {
        if (id !== fetchId.current) return;
        setKeywords([]);
        setError(err.message);
        setLoading(false);
      });
  }, [tick, projectKey]);

  const refresh = useCallback(() => setTick((n) => n + 1), []);

  return { keywords, loading, error, refresh };
}
