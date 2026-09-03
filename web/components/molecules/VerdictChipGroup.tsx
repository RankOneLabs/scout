"use client";

import { Badge } from "@/components/atoms/Badge";

const VERDICTS = ["all", "approve", "revise", "reject", "pending"] as const;

interface VerdictChipGroupProps {
  selected: string;
  onSelect: (verdict: string) => void;
}

export function VerdictChipGroup({ selected, onSelect }: VerdictChipGroupProps) {
  return (
    <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Filter by verdict">
      {VERDICTS.map((v) => {
        const value = v === "all" ? "" : v;
        const isSelected = selected === value;

        return (
          <button
            key={v}
            type="button"
            role="radio"
            aria-checked={isSelected}
            onClick={() => onSelect(value)}
            className={isSelected ? "rounded-full ring-2 ring-blue-500 ring-offset-1 dark:ring-blue-400 dark:ring-offset-gray-950" : ""}
          >
            <Badge label={v} variant="verdict" />
          </button>
        );
      })}
    </div>
  );
}
