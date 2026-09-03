"use client";

import { useState } from "react";
import { useSettingsPrompts } from "@/hooks/use-settings-prompts";
import { PromptForm } from "@/components/organisms/PromptForm";
import type { PromptTemplateListItem } from "@/types/schema";

export function PromptsTab() {
  const { prompts, loading, error, refresh } = useSettingsPrompts();
  const [editing, setEditing] = useState<PromptTemplateListItem | "new" | null>(
    null
  );

  if (editing) {
    return (
      <PromptForm
        initial={editing === "new" ? undefined : editing}
        onDone={() => {
          setEditing(null);
          refresh();
        }}
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Prompts</h2>
        <button
          type="button"
          onClick={() => setEditing("new")}
          className="rounded-md bg-blue-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-400"
        >
          New prompt
        </button>
      </div>

      {loading && (
        <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">Loading...</p>
      )}
      {error && (
        <p className="py-4 text-center text-sm text-red-600 dark:text-red-400">{error}</p>
      )}

      {!loading && !error && prompts.length === 0 && (
        <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">
          No prompts yet.
        </p>
      )}

      {!loading && prompts.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-800">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 text-xs uppercase text-gray-600 dark:text-gray-400">
              <tr>
                <th className="px-4 py-3">Name</th>
                <th className="px-4 py-3">Kind</th>
                <th className="px-4 py-3">Active</th>
                <th className="px-4 py-3">Used by</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-gray-800">
              {prompts.map((prompt) => (
                <tr
                  key={prompt.name}
                  className="bg-white dark:bg-gray-950 transition-colors hover:bg-gray-100 dark:hover:bg-gray-900"
                >
                  <td className="px-4 py-3 font-mono text-gray-800 dark:text-gray-200">
                    {prompt.name}
                  </td>
                  <td className="px-4 py-3 text-gray-600 dark:text-gray-400">{prompt.kind}</td>
                  <td className="px-4 py-3">
                    <span
                      aria-hidden="true"
                      className={`inline-block h-2 w-2 rounded-full ${
                        prompt.active ? "bg-green-500 dark:bg-green-400" : "bg-gray-300 dark:bg-gray-600"
                      }`}
                    />
                    <span className="sr-only">
                      {prompt.active ? "Active" : "Inactive"}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-gray-600 dark:text-gray-400">
                    {prompt.referenced_keyword_count}
                  </td>
                  <td className="px-4 py-3">
                    <button
                      type="button"
                      onClick={() => setEditing(prompt)}
                      className="text-xs text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300"
                    >
                      Edit
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
