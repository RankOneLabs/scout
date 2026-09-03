"use client";

import { useState } from "react";
import type { ProjectRow } from "@/types/schema";

interface ProjectFormProps {
  initial?: ProjectRow;
  onDone: () => void;
}

export function ProjectForm({ initial, onDone }: ProjectFormProps) {
  const isEdit = !!initial;
  const [form, setForm] = useState({
    key: "",
    name: initial?.name ?? "",
    description: initial?.description ?? "",
    link: initial?.link ?? "",
    dossier_summary_id: initial?.dossier_summary_id ?? "",
    active: initial?.active ?? true,
  });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async () => {
    setError(null);
    if (form.link && !/^https?:\/\//i.test(form.link)) {
      setError("Link must start with http:// or https://");
      return;
    }
    setSubmitting(true);
    try {
      if (isEdit) {
        const resp = await fetch(
          `/api/settings/projects/${encodeURIComponent(initial!.key)}`,
          {
            method: "PUT",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              name: form.name,
              description: form.description,
              link: form.link,
              dossier_summary_id: form.dossier_summary_id.trim() || null,
            }),
          }
        );
        if (!resp.ok) {
          const data = (await resp.json().catch(() => ({}))) as {
            error?: string;
            errors?: string[];
          };
          setError(data.error ?? data.errors?.join(", ") ?? "Request failed.");
          return;
        }
        if (form.active !== initial!.active) {
          const patchResp = await fetch(
            `/api/settings/projects/${encodeURIComponent(initial!.key)}`,
            {
              method: "PATCH",
              headers: { "content-type": "application/json" },
              body: JSON.stringify({ active: form.active }),
            }
          );
          if (!patchResp.ok) {
            const data = (await patchResp.json().catch(() => ({}))) as {
              error?: string;
            };
            setError(data.error ?? "Failed to update active status.");
            return;
          }
        }
      } else {
        const resp = await fetch("/api/settings/projects", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            key: form.key,
            name: form.name,
            description: form.description,
            link: form.link,
            dossier_summary_id: form.dossier_summary_id.trim() || null,
          }),
        });
        if (!resp.ok) {
          const data = (await resp.json().catch(() => ({}))) as {
            error?: string;
            errors?: string[];
          };
          setError(data.error ?? data.errors?.join(", ") ?? "Request failed.");
          return;
        }
      }
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-4 rounded-lg border border-gray-300 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/70 p-5">
      <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">
        {isEdit ? `Edit: ${initial!.key}` : "New project"}
      </h3>

      {error && (
        <p className="rounded-md border border-red-300 dark:border-red-500/40 bg-red-100 dark:bg-red-500/10 px-3 py-2 text-sm text-red-700 dark:text-red-300">
          {error}
        </p>
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {!isEdit && (
          <label className="block text-sm">
            <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
              Key *
            </span>
            <input
              type="text"
              value={form.key}
              onChange={(e) => setForm((p) => ({ ...p, key: e.target.value }))}
              placeholder="my-project"
              className="w-full rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 px-3 py-1.5 text-sm text-gray-900 dark:text-gray-100 focus:border-blue-500 focus:outline-none"
            />
          </label>
        )}
        <label className="block text-sm">
          <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
            Name *
          </span>
          <input
            type="text"
            value={form.name}
            onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))}
            className="w-full rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 px-3 py-1.5 text-sm text-gray-900 dark:text-gray-100 focus:border-blue-500 focus:outline-none"
          />
        </label>
        <label className="block text-sm">
          <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
            Link
          </span>
          <input
            type="text"
            value={form.link}
            onChange={(e) => setForm((p) => ({ ...p, link: e.target.value }))}
            className="w-full rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 px-3 py-1.5 text-sm text-gray-900 dark:text-gray-100 focus:border-blue-500 focus:outline-none"
          />
        </label>
        {isEdit && (
          <label className="flex items-center gap-2 pt-6 text-sm text-gray-700 dark:text-gray-300">
            <input
              type="checkbox"
              checked={form.active}
              onChange={(e) =>
                setForm((p) => ({ ...p, active: e.target.checked }))
              }
              className="h-4 w-4"
            />
            Active
          </label>
        )}
      </div>

      <label className="block text-sm">
        <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
          Description *
        </span>
        <textarea
          value={form.description}
          onChange={(e) =>
            setForm((p) => ({ ...p, description: e.target.value }))
          }
          rows={3}
          className="w-full rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 px-3 py-2 text-sm text-gray-900 dark:text-gray-100 focus:border-blue-500 focus:outline-none"
        />
      </label>

      <label className="block text-sm">
        <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
          Dossier Summary ID
        </span>
        <input
          type="text"
          value={form.dossier_summary_id}
          onChange={(e) =>
            setForm((p) => ({ ...p, dossier_summary_id: e.target.value }))
          }
          placeholder="dossier ID (optional)"
          className="w-full rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 px-3 py-1.5 text-sm text-gray-900 dark:text-gray-100 focus:border-blue-500 focus:outline-none"
        />
      </label>

      <div className="flex items-center justify-end gap-3">
        <button
          type="button"
          onClick={onDone}
          className="rounded-md border border-gray-300 dark:border-gray-700 px-4 py-1.5 text-sm text-gray-700 dark:text-gray-300 hover:border-gray-500 hover:text-gray-900 dark:hover:text-gray-100"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={handleSubmit}
          disabled={submitting}
          className="rounded-md bg-blue-500 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-400 disabled:cursor-not-allowed disabled:bg-gray-200 dark:disabled:bg-gray-700 disabled:text-gray-400 dark:disabled:text-gray-400"
        >
          {submitting ? "Saving..." : isEdit ? "Save" : "Create"}
        </button>
      </div>
    </div>
  );
}
