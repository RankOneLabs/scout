"use client";

import { useState } from "react";
import Link from "next/link";
import { useGradeList, type GradeListQueryFilters } from "@/hooks/use-grade-list";
import { parseUtc } from "@/lib/transforms";
import type { GradeExplorerRow } from "@/types/feedback-grades";

const RELEVANCE_OPTIONS = ["correct", "false_positive", "false_negative"];
const ACTION_OPTIONS = ["accept", "fail"];
const DIMENSION_OPTIONS = [
  "contextual_understanding",
  "factual_support",
  "unsupported_implication",
  "posture",
  "tone",
  "wording",
  "usefulness",
];
const ELIGIBILITY_OPTIONS = [
  "eligible",
  "eligible_cap",
  "outside_lookback",
  "schema_version",
  "needs_regrade",
  "missing_evaluation_linkage",
  "mismatched_evaluation_identity",
  "shared_contract_invalid",
  "manual_exclude",
];

function TextFilter({
  label, value, onChange,
}: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="flex flex-col text-xs text-gray-600 dark:text-gray-500">
      {label}
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 rounded-md border border-gray-300 bg-white px-2 py-1 text-sm text-gray-700 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
      />
    </label>
  );
}

function DateFilter({
  label, value, onChange, endOfDay = false,
}: { label: string; value: string; onChange: (v: string) => void; endOfDay?: boolean }) {
  const suffix = endOfDay ? "T23:59:59.999Z" : "T00:00:00.000Z";
  return (
    <label className="flex flex-col text-xs text-gray-600 dark:text-gray-500">
      {label}
      <input
        type="date"
        value={value}
        onChange={(e) => onChange(e.target.value ? `${e.target.value}${suffix}` : "")}
        className="mt-1 rounded-md border border-gray-300 bg-white px-2 py-1 text-sm text-gray-700 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
      />
    </label>
  );
}

function SelectFilter({
  label, value, options, onChange,
}: { label: string; value: string; options: string[]; onChange: (v: string) => void }) {
  return (
    <label className="flex flex-col text-xs text-gray-600 dark:text-gray-500">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 rounded-md border border-gray-300 bg-white px-2 py-1 text-sm text-gray-700 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
      >
        <option value="">Any</option>
        {options.map((opt) => (
          <option key={opt} value={opt}>
            {opt}
          </option>
        ))}
      </select>
    </label>
  );
}

function parsePositiveInt(value: string): number | undefined {
  if (!/^\d+$/.test(value)) return undefined;
  const n = Number(value);
  return n > 0 ? n : undefined;
}

function judgmentColor(judgment: string): string {
  if (judgment === "correct")
    return "border-green-300 bg-green-100 text-green-700 dark:border-green-500/30 dark:bg-green-500/20 dark:text-green-400";
  return "border-red-300 bg-red-100 text-red-700 dark:border-red-500/30 dark:bg-red-500/20 dark:text-red-400";
}

function eligibilityColor(reason: string | null): string {
  if (reason === "eligible")
    return "border-green-300 bg-green-100 text-green-700 dark:border-green-500/30 dark:bg-green-500/20 dark:text-green-400";
  if (reason === null)
    return "border-gray-300 bg-white text-gray-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400";
  return "border-amber-300 bg-amber-100 text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/20 dark:text-amber-400";
}

function GradeRow({ row }: { row: GradeExplorerRow }) {
  return (
    <tr className="border-t border-gray-200 dark:border-gray-800">
      <td className="px-3 py-1.5">
        <Link href={`/feedback/grades/${row.grade_id}`} className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300">
          #{row.grade_id}
        </Link>
      </td>
      <td className="px-3 py-1.5 text-gray-600 dark:text-gray-400">
        {row.evaluation_id === null ? (
          <span className="italic text-gray-500 dark:text-gray-400">unlinked</span>
        ) : (
          `eval #${row.evaluation_id}`
        )}{" "}
        · post #{row.post_id}
      </td>
      <td className="px-3 py-1.5">{row.project_key ?? "—"}</td>
      <td className="px-3 py-1.5">{row.platform ?? "—"}</td>
      <td className="px-3 py-1.5">{row.posture ?? "—"}</td>
      <td className="px-3 py-1.5">{row.terminal_status ?? "—"}</td>
      <td className="px-3 py-1.5">
        <span className={`rounded-full border px-2 py-0.5 text-[10px] ${judgmentColor(row.relevance_judgment)}`}>
          {row.relevance_judgment}
        </span>
      </td>
      <td className="px-3 py-1.5">{row.action_judgment ?? "—"}</td>
      <td className="px-3 py-1.5">
        v{row.schema_version}
        {row.needs_regrade && (
          <span className="ml-1 rounded-full border border-red-300 bg-red-100 px-1.5 py-0.5 text-[10px] text-red-700 dark:border-red-500/30 dark:bg-red-500/20 dark:text-red-400">
            regrade
          </span>
        )}
      </td>
      <td className="px-3 py-1.5">
        <span className={`rounded-full border px-2 py-0.5 text-[10px] ${eligibilityColor(row.eligibility_reason)}`}>
          {row.eligibility_reason ?? "unknown"}
        </span>
      </td>
      <td className="px-3 py-1.5">
        {row.override_mode === "exclude" ? (
          <span className="rounded-full border border-orange-300 bg-orange-100 px-2 py-0.5 text-[10px] text-orange-700 dark:border-orange-500/30 dark:bg-orange-500/20 dark:text-orange-400">
            excluded
          </span>
        ) : (
          "auto"
        )}
      </td>
      <td className="px-3 py-1.5">
        {row.snapshot_use_count}
        {row.snapshot_active_use && (
          <span className="ml-1 rounded-full border border-green-300 bg-green-100 px-1.5 py-0.5 text-[10px] text-green-700 dark:border-green-500/30 dark:bg-green-500/20 dark:text-green-400">
            active
          </span>
        )}
      </td>
      <td className="px-3 py-1.5 text-gray-600 dark:text-gray-500">{parseUtc(row.last_edit_time).toLocaleDateString()}</td>
    </tr>
  );
}

export function GradeExplorer() {
  const [filters, setFilters] = useState<GradeListQueryFilters>({});
  const { data, hasMore, totalMatching, loading, error, loadMore } = useGradeList(filters);

  const set = <K extends keyof GradeListQueryFilters>(key: K, value: GradeListQueryFilters[K]) =>
    setFilters((f) => ({ ...f, [key]: value === "" || value === undefined ? undefined : value }));

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Grade Explorer</h1>

      <details className="rounded-lg border border-gray-200 bg-gray-50 p-4 dark:border-gray-800 dark:bg-gray-900" open>
        <summary className="cursor-pointer text-sm font-medium text-gray-700 dark:text-gray-300">Filters</summary>
        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          <DateFilter label="Graded from" value={filters.gradedFrom ?? ""} onChange={(v) => set("gradedFrom", v)} />
          <DateFilter
            label="Graded to"
            value={filters.gradedTo ?? ""}
            onChange={(v) => set("gradedTo", v)}
            endOfDay
          />
          <DateFilter label="Edited from" value={filters.editedFrom ?? ""} onChange={(v) => set("editedFrom", v)} />
          <DateFilter
            label="Edited to"
            value={filters.editedTo ?? ""}
            onChange={(v) => set("editedTo", v)}
            endOfDay
          />
          <SelectFilter
            label="Schema version"
            value={filters.schemaVersion ? String(filters.schemaVersion) : ""}
            options={["1", "2"]}
            onChange={(v) => set("schemaVersion", v ? (Number(v) as 1 | 2) : undefined)}
          />
          <SelectFilter
            label="Needs regrade"
            value={filters.needsRegrade === undefined ? "" : String(filters.needsRegrade)}
            options={["true", "false"]}
            onChange={(v) => set("needsRegrade", v === "" ? undefined : v === "true")}
          />
          <TextFilter label="Project" value={filters.project ?? ""} onChange={(v) => set("project", v)} />
          <TextFilter label="Platform" value={filters.platform ?? ""} onChange={(v) => set("platform", v)} />
          <TextFilter label="Posture" value={filters.posture ?? ""} onChange={(v) => set("posture", v)} />
          <TextFilter
            label="Terminal status"
            value={filters.terminalStatus ?? ""}
            onChange={(v) => set("terminalStatus", v)}
          />
          <SelectFilter
            label="Relevance judgment"
            value={filters.relevanceJudgment ?? ""}
            options={RELEVANCE_OPTIONS}
            onChange={(v) => set("relevanceJudgment", v)}
          />
          <SelectFilter
            label="Action judgment"
            value={filters.actionJudgment ?? ""}
            options={ACTION_OPTIONS}
            onChange={(v) => set("actionJudgment", v)}
          />
          <SelectFilter
            label="Failure dimension"
            value={filters.failureDimension ?? ""}
            options={DIMENSION_OPTIONS}
            onChange={(v) => set("failureDimension", v)}
          />
          <SelectFilter
            label="Eligibility reason"
            value={filters.eligibilityReason ?? ""}
            options={ELIGIBILITY_OPTIONS}
            onChange={(v) => set("eligibilityReason", v)}
          />
          <SelectFilter
            label="Override mode"
            value={filters.overrideMode ?? ""}
            options={["auto", "exclude"]}
            onChange={(v) => set("overrideMode", v)}
          />
          <SelectFilter
            label="Snapshot use"
            value={filters.snapshotUse ?? ""}
            options={["never", "historical", "active"]}
            onChange={(v) => set("snapshotUse", v)}
          />
          <SelectFilter
            label="Trace availability"
            value={filters.traceAvailability ?? ""}
            options={["available", "unavailable"]}
            onChange={(v) => set("traceAvailability", v)}
          />
          <TextFilter
            label="Scan id"
            value={filters.scanId?.toString() ?? ""}
            onChange={(v) => set("scanId", parsePositiveInt(v))}
          />
          <TextFilter
            label="Evaluation id"
            value={filters.evaluationId?.toString() ?? ""}
            onChange={(v) => set("evaluationId", parsePositiveInt(v))}
          />
        </div>
        {(Object.keys(filters).length > 0) && (
          <button
            type="button"
            onClick={() => setFilters({})}
            className="mt-3 rounded-md border border-gray-300 bg-white px-2 py-1 text-xs text-gray-600 hover:border-gray-400 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:border-gray-600"
          >
            Clear filters
          </button>
        )}
      </details>

      {error && <p className="text-sm text-red-600 dark:text-red-400">Failed to load grades: {error}</p>}

      <p className="text-xs text-gray-600 dark:text-gray-500">{totalMatching} matching grade{totalMatching === 1 ? "" : "s"}</p>

      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-gray-600 dark:text-gray-500">
              <th className="px-3 py-2 font-normal">Grade</th>
              <th className="px-3 py-2 font-normal">Identity</th>
              <th className="px-3 py-2 font-normal">Project</th>
              <th className="px-3 py-2 font-normal">Platform</th>
              <th className="px-3 py-2 font-normal">Posture</th>
              <th className="px-3 py-2 font-normal">Terminal</th>
              <th className="px-3 py-2 font-normal">Judgment</th>
              <th className="px-3 py-2 font-normal">Action</th>
              <th className="px-3 py-2 font-normal">Schema</th>
              <th className="px-3 py-2 font-normal">Eligibility</th>
              <th className="px-3 py-2 font-normal">Override</th>
              <th className="px-3 py-2 font-normal">Snapshot use</th>
              <th className="px-3 py-2 font-normal">Edited</th>
            </tr>
          </thead>
          <tbody className="text-gray-700 dark:text-gray-300">
            {data.map((row) => (
              <GradeRow key={row.grade_id} row={row} />
            ))}
          </tbody>
        </table>
        {loading && data.length === 0 && (
          <p className="p-4 text-center text-xs text-gray-600 dark:text-gray-500">Loading...</p>
        )}
        {!loading && data.length === 0 && (
          <p className="p-4 text-center text-xs italic text-gray-600 dark:text-gray-500">No grades match these filters.</p>
        )}
      </div>

      {hasMore && (
        <button
          type="button"
          onClick={loadMore}
          className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-700 hover:border-gray-400 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-gray-600"
        >
          Load more
        </button>
      )}
    </div>
  );
}
