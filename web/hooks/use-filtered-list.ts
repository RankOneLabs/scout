"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import type { Paginated } from "@/types/schema";

export interface FilteredListOptions<T> {
  path: string;
  initialFilters?: Record<string, string>;
  getLastId: (item: T) => number | string;
}

export interface FilteredListState<T> {
  items: T[];
  filters: Record<string, string>;
  setFilters: (key: string, value: string) => void;
  loading: boolean;
  error: string | null;
  hasMore: boolean;
  loadMore: () => void;
  isLoadingMore: boolean;
}

function isPaginated<T>(value: unknown): value is Paginated<T> {
  if (value === null || typeof value !== "object") return false;
  const candidate = value as Partial<Paginated<T>>;
  return Array.isArray(candidate.data) && typeof candidate.has_more === "boolean";
}

async function readPaginated<T>(res: Response): Promise<Paginated<T>> {
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const result = (await res.json()) as unknown;
  if (!isPaginated<T>(result)) throw new Error("invalid response shape");
  return result;
}

export function useFilteredList<T>(options: FilteredListOptions<T>): FilteredListState<T> {
  const { path, initialFilters = {}, getLastId } = options;
  const [items, setItems] = useState<T[]>([]);
  const [filters, setFiltersState] = useState<Record<string, string>>(initialFilters);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const fetchId = useRef(0);

  const setFilters = useCallback((key: string, value: string) => {
    setLoading(true);
    setIsLoadingMore(false);
    setFiltersState((prev) => {
      const next = { ...prev };
      if (value) next[key] = value;
      else delete next[key];
      return next;
    });
  }, []);

  useEffect(() => {
    const id = ++fetchId.current;
    // Show loading whenever filters/path change (setFilters also sets this,
    // but path-only changes bypass setFilters).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setIsLoadingMore(false);
    setError(null);
    const params = new URLSearchParams(filters);
    fetch(`${path}?${params}`)
      .then((res) => readPaginated<T>(res))
      .then((result: Paginated<T>) => {
        if (id === fetchId.current) {
          setItems(result.data);
          setHasMore(result.has_more);
          setError(null);
          setLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (id === fetchId.current) {
          setItems([]);
          setHasMore(false);
          setError(err instanceof Error ? err.message : String(err));
          setLoading(false);
        }
      });
  }, [filters, path]);

  const loadMore = useCallback(() => {
    if (!hasMore || isLoadingMore || items.length === 0) return;
    const id = fetchId.current;
    setIsLoadingMore(true);
    const lastId = getLastId(items[items.length - 1]);
    const params = new URLSearchParams({ ...filters, before_id: String(lastId) });
    fetch(`${path}?${params}`)
      .then((res) => readPaginated<T>(res))
      .then((result: Paginated<T>) => {
        if (id !== fetchId.current) return;
        setItems((prev) => [...prev, ...result.data]);
        setHasMore(result.has_more);
        setError(null);
        setIsLoadingMore(false);
      })
      .catch((err: unknown) => {
        if (id === fetchId.current) {
          setError(err instanceof Error ? err.message : String(err));
          setIsLoadingMore(false);
        }
      });
  }, [path, filters, hasMore, isLoadingMore, items, getLastId]);

  return { items, filters, setFilters, loading, error, hasMore, loadMore, isLoadingMore };
}
