"use client";

interface ChipOption {
  label: string;
  value: string;
}

interface ChipMultiSelectProps {
  options: ChipOption[];
  selected: string[];
  onChange: (next: string[]) => void;
  colorMap?: Record<string, string>;
}

const DEFAULT_COLOR =
  "bg-gray-100 text-gray-700 border-gray-300 dark:bg-gray-700/50 dark:text-gray-300 dark:border-gray-600";
const SELECTED_COLOR =
  "bg-blue-100 text-blue-700 border-blue-300 dark:bg-blue-500/20 dark:text-blue-400 dark:border-blue-500/30";

export function ChipMultiSelect({
  options,
  selected,
  onChange,
  colorMap,
}: ChipMultiSelectProps) {
  const selectedSet = new Set(selected);

  const toggle = (value: string) => {
    const next = selectedSet.has(value)
      ? selected.filter((v) => v !== value)
      : [...selected, value];
    onChange(next);
  };

  return (
    <div className="flex flex-wrap gap-2">
      {options.map((opt) => {
        const isSelected = selectedSet.has(opt.value);
        const color = isSelected
          ? colorMap?.[opt.value] ?? SELECTED_COLOR
          : DEFAULT_COLOR;
        return (
          <button
            key={opt.value}
            type="button"
            onClick={() => toggle(opt.value)}
            aria-pressed={isSelected}
            className={`rounded-full border px-3 py-1 text-xs font-medium transition-all ${color} ${
              isSelected ? "ring-1 ring-current/30" : "opacity-70 hover:opacity-100"
            }`}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
