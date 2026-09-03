"use client";

import { useState } from "react";
import type { PromptTemplateRow } from "@/types/schema";

const PROMPT_KINDS = [
  "evaluate",
  "respond",
  "critique",
  "shared",
  "custom",
] as const;

interface PromptFormProps {
  initial?: PromptTemplateRow;
  onDone: () => void;
}

export function PromptForm({ initial, onDone }: PromptFormProps) {
  const isEdit = !!initial;
  const [form, setForm] = useState({
    name: "",
    body: initial?.body ?? "",
    kind: (initial?.kind ?? "evaluate") as (typeof PROMPT_KINDS)[number],
    active: initial?.active ?? true,
  });
  const [error, setError] = useState<string | null>(null);
  const [conflictMessage, setConflictMessage] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (opts?: { confirmDeactivate?: boolean }) => {
    setError(null);
    setConflictMessage(null);
    setSubmitting(true);
    try {
      if (isEdit) {
        const resp = await fetch(
          `/api/settings/prompts/${encodeURIComponent(initial!.name)}`,
          {
            method: "PUT",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ body: form.body, kind: form.kind }),
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
          const patchBody: { active: boolean; confirm?: boolean } = {
            active: form.active,
          };
          if (!form.active && opts?.confirmDeactivate) {
            patchBody.confirm = true;
          }
          const patchResp = await fetch(
            `/api/settings/prompts/${encodeURIComponent(initial!.name)}`,
            {
              method: "PATCH",
              headers: { "content-type": "application/json" },
              body: JSON.stringify(patchBody),
            }
          );
          if (!patchResp.ok) {
            const data = (await patchResp.json().catch(() => ({}))) as {
              error?: string;
            };
            if (patchResp.status === 409) {
              setConflictMessage(
                `Changes to body/kind were saved. ${data.error ?? "This prompt is referenced by active keywords."} Deactivating requires confirmation.`
              );
              return;
            }
            setError(data.error ?? "Failed to update active status.");
            return;
          }
        }
      } else {
        const resp = await fetch("/api/settings/prompts", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            name: form.name,
            body: form.body,
            kind: form.kind,
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
        {isEdit ? `Edit: ${initial!.name}` : "New prompt"}
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
              Name *
            </span>
            <input
              type="text"
              value={form.name}
              onChange={(e) =>
                setForm((p) => ({ ...p, name: e.target.value }))
              }
              placeholder="my_prompt"
              className="w-full rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 px-3 py-1.5 text-sm text-gray-900 dark:text-gray-100 focus:border-blue-500 focus:outline-none"
            />
          </label>
        )}

        <label className="block text-sm">
          <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
            Kind *
          </span>
          <select
            value={form.kind}
            onChange={(e) =>
              setForm((p) => ({
                ...p,
                kind: e.target.value as (typeof PROMPT_KINDS)[number],
              }))
            }
            className="w-full rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 px-3 py-1.5 text-sm text-gray-900 dark:text-gray-100 focus:border-blue-500 focus:outline-none"
          >
            {PROMPT_KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
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
          Body *
        </span>
        <textarea
          value={form.body}
          onChange={(e) => {
            setForm((p) => ({ ...p, body: e.target.value }));
            setError(null);
          }}
          rows={8}
          className="w-full rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 px-3 py-2 font-mono text-sm text-gray-900 dark:text-gray-100 focus:border-blue-500 focus:outline-none"
        />
      </label>

      {conflictMessage && (
        <div className="rounded-md border border-amber-300 dark:border-amber-500/40 bg-amber-100 dark:bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-300">
          <p>{conflictMessage}</p>
          <button
            type="button"
            onClick={() => void handleSubmit({ confirmDeactivate: true })}
            disabled={submitting}
            className="mt-2 rounded-md bg-amber-600 px-3 py-1 text-xs font-medium text-white hover:bg-amber-500 disabled:opacity-50"
          >
            Force deactivate
          </button>
        </div>
      )}

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
          onClick={() => void handleSubmit()}
          disabled={submitting}
          className="rounded-md bg-blue-500 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-400 disabled:cursor-not-allowed disabled:bg-gray-200 dark:disabled:bg-gray-700 disabled:text-gray-400 dark:disabled:text-gray-400"
        >
          {submitting ? "Saving..." : isEdit ? "Save" : "Create"}
        </button>
      </div>
    </div>
  );
}
