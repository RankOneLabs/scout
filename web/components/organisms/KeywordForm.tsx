"use client";

import { useState } from "react";
import type { ProjectKeywordRow, ProjectRow, PromptTemplateListItem } from "@/types/schema";

interface KeywordFormProps {
  initial?: ProjectKeywordRow;
  projects: ProjectRow[];
  prompts: PromptTemplateListItem[];
  defaultProjectKey?: string;
  onDone: () => void;
}

type KeywordFormState = {
  project_key: string;
  keyword: string;
  match_type: ProjectKeywordRow["match_type"];
  intent: string;
  positive_context: string;
  negative_context: string;
  notes: string;
  priority: string;
  evaluate_prompt: string;
  respond_prompt: string;
  critique_prompt: string;
  active: boolean;
};

const splitContextLines = (value: string) =>
  value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

const joinContextLines = (value: string[]) => value.join("\n");

const nullableText = (value: string) => {
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
};

const splitKeywordInput = (value: string) =>
  value
    .split(",")
    .map((keyword) => keyword.trim())
    .filter(Boolean);

export function KeywordForm({
  initial,
  projects,
  prompts,
  defaultProjectKey,
  onDone,
}: KeywordFormProps) {
  const isEdit = !!initial;
  const [form, setForm] = useState<KeywordFormState>({
    project_key: initial?.project_key ?? defaultProjectKey ?? "",
    keyword: initial?.keyword ?? "",
    match_type: initial?.match_type ?? "substring",
    intent: initial?.intent ?? "",
    positive_context: joinContextLines(initial?.positive_context ?? []),
    negative_context: joinContextLines(initial?.negative_context ?? []),
    notes: initial?.notes ?? "",
    priority: String(initial?.priority ?? 100),
    evaluate_prompt: initial?.evaluate_prompt ?? "",
    respond_prompt: initial?.respond_prompt ?? "",
    critique_prompt: initial?.critique_prompt ?? "",
    active: initial?.active ?? true,
  });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const promptsForKind = (kind: string, currentValue: string) =>
    prompts.filter(
      (p) =>
        (p.kind === kind || p.kind === "shared" || p.kind === "custom") &&
        (p.active || p.name === currentValue)
    );

  const submitKeyword = async () => {
    setError(null);
    const projectKey = form.project_key.trim();
    const keyword = form.keyword.trim();
    if (!projectKey) {
      setError("Project is required.");
      return;
    }
    if (!keyword) {
      setError("Keyword is required.");
      return;
    }
    const keywords = splitKeywordInput(keyword);
    if (!isEdit && keywords.length === 0) {
      setError("At least one keyword is required.");
      return;
    }
    setSubmitting(true);
    const payload = {
      project_key: projectKey,
      match_type: form.match_type,
      intent: nullableText(form.intent),
      positive_context: splitContextLines(form.positive_context),
      negative_context: splitContextLines(form.negative_context),
      notes: nullableText(form.notes),
      priority: form.priority === "" ? undefined : Number(form.priority),
      evaluate_prompt: form.evaluate_prompt || null,
      respond_prompt: form.respond_prompt || null,
      critique_prompt: form.critique_prompt || null,
      active: form.active,
    };
    try {
      if (isEdit) {
        const putResp = await fetch(`/api/settings/keywords/${initial!.id}`, {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ ...payload, keyword }),
        });
        if (!putResp.ok) {
          const data = (await putResp.json().catch(() => ({}))) as {
            error?: string;
            errors?: string[];
          };
          setError(data.error ?? data.errors?.join(", ") ?? "Request failed.");
          return;
        }
      } else {
        const postResp = await fetch("/api/settings/keywords", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ ...payload, keywords }),
        });
        if (!postResp.ok) {
          const data = (await postResp.json().catch(() => ({}))) as {
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

  const promptSelect = (
    field: "evaluate_prompt" | "respond_prompt" | "critique_prompt",
    label: string,
    kind: string
  ) => (
    <label className="block text-sm">
      <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
        {label}
      </span>
      <select
        value={form[field]}
        onChange={(e) => setForm((p) => ({ ...p, [field]: e.target.value }))}
        className="w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100"
      >
        <option value="">— use scan default —</option>
        {promptsForKind(kind, form[field]).map((p) => (
          <option key={p.name} value={p.name}>
            {p.name}
            {!p.active ? " (inactive)" : ""}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <div className="space-y-4 rounded-lg border border-gray-300 bg-gray-50 p-5 dark:border-gray-700 dark:bg-gray-900/70">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">
            {isEdit ? `Edit keyword: ${initial!.keyword}` : "New keyword"}
          </h3>
          <p className="mt-1 text-xs text-gray-600 dark:text-gray-500">
            Routing metadata drives matching. Prompt overrides stay available
            below as advanced settings.
          </p>
        </div>
        {isEdit && (
          <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
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

      {error && (
        <p className="rounded-md border border-red-300 bg-red-100 px-3 py-2 text-sm text-red-700 dark:border-red-500/40 dark:bg-red-500/10 dark:text-red-300">
          {error}
        </p>
      )}

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <label className="block text-sm">
          <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
            Project *
          </span>
          <select
            value={form.project_key}
            onChange={(e) => setForm((p) => ({ ...p, project_key: e.target.value }))}
            className="w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100"
          >
            <option value="">Select project</option>
            {projects.map((p) => (
              <option key={p.key} value={p.key}>
                {p.name} ({p.key}){p.active ? "" : " (inactive)"}
              </option>
            ))}
          </select>
        </label>

        <label className="block text-sm">
          <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
            {isEdit ? "Keyword *" : "Keyword(s) *"}
          </span>
          <input
            type="text"
            value={form.keyword}
            onChange={(e) =>
              setForm((p) => ({ ...p, keyword: e.target.value }))
            }
            placeholder={isEdit ? undefined : "postgres, vector database, RAG"}
            className="w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100"
          />
        </label>

        <label className="block text-sm">
          <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
            Match type
          </span>
          <select
            value={form.match_type}
            onChange={(e) =>
              setForm((p) => ({
                ...p,
                match_type: e.target.value as KeywordFormState["match_type"],
              }))
            }
            className="w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100"
          >
            <option value="substring">Substring</option>
            <option value="phrase">Phrase</option>
            <option value="exact">Exact</option>
            <option value="regex">Regex</option>
          </select>
        </label>

        <label className="block text-sm">
          <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
            Priority
          </span>
          <input
            type="number"
            min={0}
            step={1}
            value={form.priority}
            onChange={(e) =>
              setForm((p) => ({ ...p, priority: e.target.value }))
            }
            className="w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100"
          />
        </label>

        <label className="block text-sm md:col-span-2">
          <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
            Intent
          </span>
          <textarea
            value={form.intent}
            onChange={(e) => setForm((p) => ({ ...p, intent: e.target.value }))}
            rows={3}
            className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100"
            placeholder="Short routing intent"
          />
        </label>

        <label className="block text-sm md:col-span-2">
          <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
            Positive context
          </span>
          <textarea
            value={form.positive_context}
            onChange={(e) =>
              setForm((p) => ({ ...p, positive_context: e.target.value }))
            }
            rows={4}
            className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100"
            placeholder="One positive context phrase per line"
          />
        </label>

        <label className="block text-sm md:col-span-2">
          <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
            Negative context
          </span>
          <textarea
            value={form.negative_context}
            onChange={(e) =>
              setForm((p) => ({ ...p, negative_context: e.target.value }))
            }
            rows={4}
            className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100"
            placeholder="One negative context phrase per line"
          />
        </label>

        <label className="block text-sm md:col-span-2">
          <span className="mb-1 block text-xs uppercase tracking-wide text-gray-600 dark:text-gray-500">
            Notes
          </span>
          <textarea
            value={form.notes}
            onChange={(e) => setForm((p) => ({ ...p, notes: e.target.value }))}
            rows={3}
            className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100"
            placeholder="Internal notes for operators"
          />
        </label>
      </div>

      <details className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-950/50">
        <summary className="cursor-pointer text-sm font-medium text-gray-800 dark:text-gray-200">
          Advanced prompt overrides
        </summary>
        <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
          {promptSelect("evaluate_prompt", "Evaluate prompt", "evaluate")}
          {promptSelect("respond_prompt", "Respond prompt", "respond")}
          {promptSelect("critique_prompt", "Critique prompt", "critique")}
        </div>
      </details>

      <div className="flex items-center justify-end gap-3">
        <button
          type="button"
          onClick={onDone}
          className="rounded-md border border-gray-300 px-4 py-1.5 text-sm text-gray-700 hover:border-gray-500 hover:text-gray-900 dark:border-gray-700 dark:text-gray-300 dark:hover:text-gray-100"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={submitKeyword}
          disabled={submitting}
          className="rounded-md bg-blue-500 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-400 disabled:cursor-not-allowed disabled:bg-gray-200 disabled:text-gray-400 dark:disabled:bg-gray-700 dark:disabled:text-gray-400"
        >
          {submitting ? "Saving..." : isEdit ? "Save" : "Create"}
        </button>
      </div>
    </div>
  );
}
