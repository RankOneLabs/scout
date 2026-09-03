"use client";

import { usePosts } from "@/hooks/use-posts";
import { PostList } from "@/components/organisms/PostList";

export default function PostsPage() {
  const { posts, filters, setFilters, loading, error, hasMore, loadMore, isLoadingMore } =
    usePosts();

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">Loading...</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Posts</h1>
      {error && (
        <div className="rounded-lg border border-red-300 bg-red-100 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          Failed to load posts: {error}
        </div>
      )}
      <PostList
        posts={posts}
        filters={filters}
        onFilterChange={setFilters}
        hasMore={hasMore}
        onLoadMore={loadMore}
        isLoadingMore={isLoadingMore}
      />
    </div>
  );
}
