"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { PromptTemplateListItem } from "@/types/schema";

interface UseSettingsPromptsState {
  prompts: PromptTemplateListItem[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useSettingsPrompts(): UseSettingsPromptsState {
  const [prompts, setPrompts] = useState<PromptTemplateListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fetchId = useRef(0);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const id = ++fetchId.current;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    fetch("/api/settings/prompts?include_inactive=true")
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return (await res.json()) as { prompts: PromptTemplateListItem[] };
      })
      .then((body) => {
        if (id !== fetchId.current) return;
        setPrompts(body.prompts ?? []);
        setError(null);
        setLoading(false);
      })
      .catch((err: Error) => {
        if (id !== fetchId.current) return;
        setPrompts([]);
        setError(err.message);
        setLoading(false);
      });
  }, [tick]);

  const refresh = useCallback(() => setTick((n) => n + 1), []);

  return { prompts, loading, error, refresh };
}
