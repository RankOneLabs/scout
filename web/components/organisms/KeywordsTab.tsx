"use client";

import { useState } from "react";
import { useSettingsKeywords } from "@/hooks/use-settings-keywords";
import { useSettingsProjects } from "@/hooks/use-settings-projects";
import { useSettingsPrompts } from "@/hooks/use-settings-prompts";
import { KeywordForm } from "@/components/organisms/KeywordForm";
import type { ProjectKeywordRow } from "@/types/schema";

const previewItems = (items: string[]) => items.slice(0, 2).join("; ");

export function KeywordsTab() {
  const [projectKey, setProjectKey] = useState<string | undefined>(undefined);
  const { keywords, loading, error, refresh } = useSettingsKeywords(projectKey);
  const { projects } = useSettingsProjects();
  const { prompts } = useSettingsPrompts();
  const [editing, setEditing] = useState<ProjectKeywordRow | "new" | null>(null);

  if (editing) {
    return (
      <KeywordForm
        initial={editing === "new" ? undefined : editing}
        projects={projects}
        prompts={prompts}
        defaultProjectKey={projectKey}
        onDone={() => {
          setEditing(null);
          refresh();
        }}
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Keywords</h2>
          <select
            value={projectKey ?? ""}
            onChange={(e) => setProjectKey(e.target.value || undefined)}
            className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100"
          >
            <option value="">All projects</option>
            {projects.map((p) => (
              <option key={p.key} value={p.key}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
        <button
          type="button"
          onClick={() => setEditing("new")}
          className="rounded-md bg-blue-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-400"
        >
          New keyword
        </button>
      </div>

      {loading && (
        <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">Loading...</p>
      )}
      {error && (
        <p className="py-4 text-center text-sm text-red-600 dark:text-red-400">{error}</p>
      )}

      {!loading && !error && keywords.length === 0 && (
        <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">
          No keywords yet.
        </p>
      )}

      {!loading && keywords.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-800">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-gray-200 bg-gray-50 text-xs uppercase text-gray-600 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400">
              <tr>
                <th className="px-4 py-3">Keyword</th>
                <th className="px-4 py-3">Project</th>
                <th className="px-4 py-3">Match</th>
                <th className="px-4 py-3">Intent</th>
                <th className="px-4 py-3">Context</th>
                <th className="px-4 py-3">Priority</th>
                <th className="px-4 py-3">Active</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-gray-800">
              {keywords.map((kw) => (
                <tr
                  key={kw.id}
                  className="bg-white transition-colors hover:bg-gray-100 dark:bg-gray-950 dark:hover:bg-gray-900"
                >
                  <td className="px-4 py-3 text-gray-800 dark:text-gray-200">{kw.keyword}</td>
                  <td className="px-4 py-3 font-mono text-gray-600 dark:text-gray-400">
                    {kw.project_key}
                  </td>
                  <td className="px-4 py-3 text-gray-600 dark:text-gray-400">{kw.match_type}</td>
                  <td className="px-4 py-3">
                    <div
                      className="max-w-xs truncate text-gray-700 dark:text-gray-300"
                      title={kw.intent ?? ""}
                    >
                      {kw.intent ?? "—"}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-gray-600 dark:text-gray-400">
                    <div>
                      <span className="font-medium text-gray-800 dark:text-gray-200">
                        +{kw.positive_context.length}
                      </span>
                      <span className="text-gray-600 dark:text-gray-500"> / </span>
                      <span className="font-medium text-gray-800 dark:text-gray-200">
                        -{kw.negative_context.length}
                      </span>
                    </div>
                    <div className="mt-1 max-w-xs truncate text-xs text-gray-600 dark:text-gray-500" title={previewItems(kw.positive_context)}>
                      {previewItems(kw.positive_context) || "No positive context"}
                    </div>
                    <div className="max-w-xs truncate text-xs text-gray-600 dark:text-gray-500" title={previewItems(kw.negative_context)}>
                      {previewItems(kw.negative_context) || "No negative context"}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-gray-600 dark:text-gray-400">{kw.priority}</td>
                  <td className="px-4 py-3">
                    <span
                      aria-hidden="true"
                      className={`inline-block h-2 w-2 rounded-full ${
                        kw.active ? "bg-green-600 dark:bg-green-400" : "bg-gray-300 dark:bg-gray-600"
                      }`}
                    />
                    <span className="sr-only">
                      {kw.active ? "Active" : "Inactive"}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <button
                      type="button"
                      onClick={() => setEditing(kw)}
                      className="text-xs text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
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
