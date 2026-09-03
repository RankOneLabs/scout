"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ProjectListItem } from "@/types/schema";

interface UseSettingsProjectsState {
  projects: ProjectListItem[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useSettingsProjects(): UseSettingsProjectsState {
  const [projects, setProjects] = useState<ProjectListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fetchId = useRef(0);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const id = ++fetchId.current;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    fetch("/api/settings/projects?include_inactive=true")
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return (await res.json()) as { projects: ProjectListItem[] };
      })
      .then((body) => {
        if (id !== fetchId.current) return;
        setProjects(body.projects ?? []);
        setError(null);
        setLoading(false);
      })
      .catch((err: Error) => {
        if (id !== fetchId.current) return;
        setProjects([]);
        setError(err.message);
        setLoading(false);
      });
  }, [tick]);

  const refresh = useCallback(() => setTick((n) => n + 1), []);

  return { projects, loading, error, refresh };
}
