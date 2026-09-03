"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { BlockedAuthorRow } from "@/types/schema";

export function useBlockedAuthors() {
  const [blockedAuthors, setBlockedAuthors] = useState<BlockedAuthorRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fetchId = useRef(0);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const id = ++fetchId.current;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    fetch("/api/settings/blocked-authors")
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return (await response.json()) as {
          blocked_authors: BlockedAuthorRow[];
        };
      })
      .then((body) => {
        if (id !== fetchId.current) return;
        setBlockedAuthors(body.blocked_authors ?? []);
        setError(null);
        setLoading(false);
      })
      .catch((cause: Error) => {
        if (id !== fetchId.current) return;
        setBlockedAuthors([]);
        setError(cause.message);
        setLoading(false);
      });
  }, [tick]);

  const refresh = useCallback(() => setTick((value) => value + 1), []);
  return { blockedAuthors, loading, error, refresh };
}
