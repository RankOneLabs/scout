"use client";

import { useState, useEffect } from "react";
import type { ScanStats } from "@/types/schema";

export function useStats() {
  const [stats, setStats] = useState<ScanStats | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/api/stats")
      .then((res) => {
        if (!res.ok) return null;
        return res.json();
      })
      .then((data) => setStats(data))
      .catch(() => setStats(null))
      .finally(() => setLoading(false));
  }, []);

  return { stats, loading };
}
