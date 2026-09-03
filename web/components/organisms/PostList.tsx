"use client";

import type { PostWithEvaluation } from "@/types/schema";
import { PostPreview } from "@/components/molecules/PostPreview";
import { FilterBar } from "@/components/molecules/FilterBar";

interface PostListProps {
  posts: PostWithEvaluation[];
  filters: Record<string, string>;
  onFilterChange: (key: string, value: string) => void;
  showFilters?: boolean;
  hasMore?: boolean;
  onLoadMore?: () => void;
  isLoadingMore?: boolean;
}

const FILTER_DEFS = [
  {
    key: "platform",
    label: "All Platforms",
    options: [
      { label: "Discord", value: "discord" },
      { label: "Farcaster", value: "farcaster" },
      { label: "Bluesky", value: "bluesky" },
    ],
  },
  {
    key: "relevant",
    label: "All Relevance",
    options: [
      { label: "Relevant", value: "true" },
      { label: "Not Relevant", value: "false" },
    ],
  },
];

export function PostList({
  posts,
  filters,
  onFilterChange,
  showFilters = true,
  hasMore = false,
  onLoadMore,
  isLoadingMore = false,
}: PostListProps) {
  return (
    <div className="space-y-4">
      {showFilters && (
        <FilterBar
          filters={FILTER_DEFS}
          values={filters}
          onChange={onFilterChange}
        />
      )}
      {posts.length === 0 ? (
        <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">No posts found.</p>
      ) : (
        <div className="space-y-3">
          {posts.map((post) => (
            <PostPreview key={post.id} post={post} />
          ))}
          {hasMore && onLoadMore && (
            <button
              type="button"
              onClick={onLoadMore}
              disabled={isLoadingMore}
              className="w-full rounded-lg border border-gray-300 py-2 text-sm text-gray-600 transition-colors hover:border-gray-500 hover:text-gray-800 disabled:opacity-50 dark:border-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
            >
              {isLoadingMore ? "Loading..." : "Load more"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
