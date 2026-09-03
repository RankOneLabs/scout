"use client";

import { useState } from "react";
import type { GradeDetailOverride } from "@/types/feedback-grades";

interface GradeOverrideControlProps {
  evaluationId: number | null;
  override: GradeDetailOverride | null;
  onChanged: () => void;
}

// Usage-override controls proxy through cohort 1's evaluation-scoped
// trusted sidecar route (POST /api/grades/{evaluationId}/usage-override).
// Only auto and exclude exist — there is no force-include: auto simply
// lets the classifier decide, it cannot repair an otherwise-invalid grade.
export function GradeOverrideControl({ evaluationId, override, onChanged }: GradeOverrideControlProps) {
  const [reason, setReason] = useState("");
  const [showReasonInput, setShowReasonInput] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (evaluationId === null) {
    return (
      <p className="text-xs italic text-gray-600 dark:text-gray-500">
        Override unavailable — this grade is not linked to an evaluation.
      </p>
    );
  }

  const currentMode = override?.mode ?? "auto";

  const save = async (mode: "auto" | "exclude", excludeReason?: string) => {
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(`/api/grades/${evaluationId}/usage-override`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(mode === "exclude" ? { mode, reason: excludeReason } : { mode }),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { errors?: unknown; detail?: string };
        const errors = Array.isArray(body.errors)
          ? body.errors.filter((e): e is string => typeof e === "string")
          : [];
        setError(errors.join(" ") || body.detail || `Save failed (${res.status})`);
        return;
      }
      setShowReasonInput(false);
      setReason("");
      onChanged();
    } catch {
      setError("Override could not be saved — check your connection");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wide ${
            currentMode === "exclude"
              ? "border-orange-300 bg-orange-100 text-orange-700 dark:border-orange-500/30 dark:bg-orange-500/20 dark:text-orange-400"
              : "border-gray-300 bg-white text-gray-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400"
          }`}
        >
          {currentMode}
        </span>
        {override?.reason && <span className="text-xs text-gray-600 dark:text-gray-400">{override.reason}</span>}
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={saving || currentMode === "auto"}
          onClick={() => save("auto")}
          className="rounded-md border border-gray-300 bg-white px-2.5 py-1 text-xs text-gray-700 hover:border-gray-400 disabled:opacity-40 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-gray-600"
        >
          Set auto
        </button>
        <button
          type="button"
          disabled={saving}
          onClick={() => setShowReasonInput(true)}
          className="rounded-md border border-orange-300 bg-orange-50 px-2.5 py-1 text-xs text-orange-700 hover:bg-orange-100 disabled:opacity-40 dark:border-orange-500/30 dark:bg-orange-500/10 dark:text-orange-400 dark:hover:bg-orange-500/20"
        >
          Exclude
        </button>
      </div>

      {showReasonInput && (
        <div className="space-y-1.5">
          <input
            type="text"
            placeholder="Exclude reason (required)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 placeholder-gray-400 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:placeholder-gray-600"
          />
          <button
            type="button"
            disabled={saving || reason.trim().length === 0}
            onClick={() => save("exclude", reason.trim())}
            className="rounded-md border border-gray-300 bg-white px-2.5 py-1 text-xs text-gray-700 hover:border-gray-400 disabled:opacity-40 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-gray-600"
          >
            Save exclude
          </button>
        </div>
      )}

      {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
      <p className="text-[11px] text-gray-500 dark:text-gray-400">
        Auto lets the classifier decide — it does not force inclusion of an otherwise-invalid grade.
      </p>
    </div>
  );
}
