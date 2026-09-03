"use client";

import { useRef, useState } from "react";
import { useBlockedAuthors } from "@/hooks/use-blocked-authors";

export function BlockedAuthorsTab() {
  const { blockedAuthors, loading, error, refresh } = useBlockedAuthors();
  const [removingId, setRemovingId] = useState<number | null>(null);
  const removingIdRef = useRef<number | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);

  const unblock = async (id: number) => {
    if (removingIdRef.current !== null) return;
    removingIdRef.current = id;
    setRemovingId(id);
    setMutationError(null);
    try {
      const response = await fetch(`/api/settings/blocked-authors/${id}`, {
        method: "DELETE",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      refresh();
    } catch (cause) {
      setMutationError(cause instanceof Error ? cause.message : "unblock failed");
    } finally {
      removingIdRef.current = null;
      setRemovingId(null);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Blocked authors</h2>
        <p className="text-sm text-gray-600 dark:text-gray-500">
          Block accounts from any draft or evaluation card. Their posts remain
          stored for audit and deduplication, but Scout skips all model calls.
        </p>
      </div>

      {loading && <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">Loading...</p>}
      {(error || mutationError) && (
        <p className="py-4 text-center text-sm text-red-700 dark:text-red-400">
          {mutationError ?? error}
        </p>
      )}
      {!loading && !error && blockedAuthors.length === 0 && (
        <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">
          No blocked authors.
        </p>
      )}
      {!loading && blockedAuthors.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-800">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-gray-200 bg-gray-50 text-xs uppercase text-gray-600 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400">
              <tr>
                <th className="px-4 py-3">Author</th>
                <th className="px-4 py-3">Platform</th>
                <th className="px-4 py-3">Stable ID</th>
                <th className="px-4 py-3">Reason</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-gray-800">
              {blockedAuthors.map((author) => (
                <tr key={author.id} className="bg-white dark:bg-gray-950">
                  <td className="px-4 py-3 text-gray-800 dark:text-gray-200">
                    {author.author_name ?? "unknown"}
                  </td>
                  <td className="px-4 py-3 text-gray-600 dark:text-gray-400">{author.platform}</td>
                  <td className="max-w-sm truncate px-4 py-3 font-mono text-xs text-gray-600 dark:text-gray-500" title={author.author_id}>
                    {author.author_id}
                  </td>
                  <td className="px-4 py-3 text-gray-600 dark:text-gray-400">{author.reason ?? "—"}</td>
                  <td className="px-4 py-3">
                    <button
                      type="button"
                      onClick={() => unblock(author.id)}
                      disabled={removingId !== null}
                      className="text-xs text-blue-600 hover:text-blue-500 disabled:text-gray-400 dark:text-blue-400 dark:hover:text-blue-300 dark:disabled:text-gray-600"
                    >
                      {removingId === author.id ? "Unblocking…" : "Unblock"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
