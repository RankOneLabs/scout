"use client";

import { useState } from "react";
import { ExternalLink } from "@/components/atoms/ExternalLink";
import { useSettingsProjects } from "@/hooks/use-settings-projects";
import { ProjectForm } from "@/components/organisms/ProjectForm";
import type { ProjectListItem } from "@/types/schema";

export function ProjectsTab() {
  const { projects, loading, error, refresh } = useSettingsProjects();
  const [editing, setEditing] = useState<ProjectListItem | "new" | null>(null);

  if (editing) {
    return (
      <ProjectForm
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
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Projects</h2>
        <button
          type="button"
          onClick={() => setEditing("new")}
          className="rounded-md bg-blue-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-400"
        >
          New project
        </button>
      </div>

      {loading && (
        <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">Loading...</p>
      )}
      {error && (
        <p className="py-4 text-center text-sm text-red-600 dark:text-red-400">{error}</p>
      )}

      {!loading && !error && projects.length === 0 && (
        <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">
          No projects yet.
        </p>
      )}

      {!loading && projects.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-800">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 text-xs uppercase text-gray-600 dark:text-gray-400">
              <tr>
                <th className="px-4 py-3">Key</th>
                <th className="px-4 py-3">Name</th>
                <th className="px-4 py-3">Description</th>
                <th className="px-4 py-3">Link</th>
                <th className="px-4 py-3">Dossier</th>
                <th className="px-4 py-3">Active</th>
                <th className="px-4 py-3">Keywords</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-gray-800">
              {projects.map((project) => (
                <tr
                  key={project.key}
                  className="bg-white dark:bg-gray-950 transition-colors hover:bg-gray-100 dark:hover:bg-gray-900"
                >
                  <td className="px-4 py-3 font-mono text-gray-700 dark:text-gray-300">
                    {project.key}
                  </td>
                  <td className="px-4 py-3 text-gray-800 dark:text-gray-200">{project.name}</td>
                  <td className="max-w-xs truncate px-4 py-3 text-gray-600 dark:text-gray-400">
                    {project.description.length > 80
                      ? `${project.description.slice(0, 80)}…`
                      : project.description}
                  </td>
                  <td className="px-4 py-3">
                    {project.link ? (
                      <ExternalLink href={project.link}>
                        {project.link.replace(/^https?:\/\//, "")}
                      </ExternalLink>
                    ) : (
                      <span className="text-xs text-gray-500 dark:text-gray-600">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {project.dossier_summary_id ? (
                      <span className="font-mono text-xs text-green-600 dark:text-green-400" title={project.dossier_summary_id}>
                        {project.dossier_summary_id.slice(0, 8)}&hellip;
                      </span>
                    ) : (
                      <span className="text-xs text-gray-500 dark:text-gray-600">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <span
                      aria-hidden="true"
                      className={`inline-block h-2 w-2 rounded-full ${
                        project.active ? "bg-green-500 dark:bg-green-400" : "bg-gray-300 dark:bg-gray-600"
                      }`}
                    />
                    <span className="sr-only">
                      {project.active ? "Active" : "Inactive"}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-gray-600 dark:text-gray-400">
                    {project.keyword_count}
                  </td>
                  <td className="px-4 py-3">
                    <button
                      type="button"
                      onClick={() => setEditing(project)}
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
