"use client";

import { useEffect, useState } from "react";
import type { DraftWithContext, DraftWithGrade, Grade } from "@/types/schema";
import { DraftCard } from "@/components/organisms/DraftCard";
import { VerdictChipGroup } from "@/components/molecules/VerdictChipGroup";
import { FilterBar } from "@/components/molecules/FilterBar";

interface DraftListProps {
  drafts: DraftWithContext[] | DraftWithGrade[];
  filters: Record<string, string>;
  onFilterChange: (key: string, value: string) => void;
  showFilters?: boolean;
  onGradeUpdate?: (evaluationId: number, grade: Grade) => void;
  hasMore?: boolean;
  onLoadMore?: () => void;
  isLoadingMore?: boolean;
}

function useProjectFilter() {
  const [options, setOptions] = useState<
    Array<{ label: string; value: string }>
  >([]);

  useEffect(() => {
    fetch("/api/projects")
      .then((res) => {
        if (!res.ok) return [];
        return res.json();
      })
      .then((keys: string[]) => {
        setOptions(keys.map((k) => ({ label: k, value: k })));
      })
      .catch(() => {});
  }, []);

  return {
    key: "project_key",
    label: "All Projects",
    options,
  };
}

function asDraftWithGrade(draft: DraftWithContext | DraftWithGrade): DraftWithGrade {
  if ("grade" in draft) return draft;
  return { ...draft, grade: null };
}

export function DraftList({
  drafts,
  filters,
  onFilterChange,
  showFilters = true,
  onGradeUpdate,
  hasMore = false,
  onLoadMore,
  isLoadingMore = false,
}: DraftListProps) {
  const projectFilter = useProjectFilter();
  return (
    <div className="space-y-4">
      {showFilters && (
        <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center sm:gap-4">
          <VerdictChipGroup
            selected={filters.verdict ?? ""}
            onSelect={(v) => onFilterChange("verdict", v)}
          />
          <FilterBar
            filters={[projectFilter]}
            values={filters}
            onChange={onFilterChange}
          />
        </div>
      )}
      {drafts.length === 0 ? (
        <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">
          No drafts found.
        </p>
      ) : (
        <div className="space-y-3">
          {drafts.map((draft) => (
            <DraftCard
              key={draft.draft_id}
              draft={asDraftWithGrade(draft)}
              onGradeUpdate={onGradeUpdate}
            />
          ))}
          {hasMore && onLoadMore && (
            <button
              type="button"
              onClick={onLoadMore}
              disabled={isLoadingMore}
              className="w-full rounded-lg border border-gray-300 dark:border-gray-700 py-2 text-sm text-gray-600 dark:text-gray-400 transition-colors hover:border-gray-500 hover:text-gray-900 dark:hover:text-gray-200 disabled:opacity-50"
            >
              {isLoadingMore ? "Loading..." : "Load more"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
