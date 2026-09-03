"use client";

import type { PostWithEvaluation } from "@/types/schema";
import { useFilteredList } from "./use-filtered-list";

const getPostId = (p: PostWithEvaluation) => p.id;

export function usePosts(initialFilters: Record<string, string> = {}) {
  const { items, filters, setFilters, loading, error, hasMore, loadMore, isLoadingMore } =
    useFilteredList<PostWithEvaluation>({
      path: "/api/posts",
      initialFilters,
      getLastId: getPostId,
    });
  return { posts: items, filters, setFilters, loading, error, hasMore, loadMore, isLoadingMore };
}
