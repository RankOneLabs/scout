"use client";

import type { DraftWithContext } from "@/types/schema";
import { useFilteredList } from "./use-filtered-list";

const getDraftId = (d: DraftWithContext) => d.draft_id;

export function useDrafts(initialFilters: Record<string, string> = {}) {
  const { items, filters, setFilters, loading, error, hasMore, loadMore, isLoadingMore } =
    useFilteredList<DraftWithContext>({
      path: "/api/drafts",
      initialFilters,
      getLastId: getDraftId,
    });
  return { drafts: items, filters, setFilters, loading, error, hasMore, loadMore, isLoadingMore };
}
