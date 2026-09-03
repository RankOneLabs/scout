"use client";

import type { ReactNode } from "react";

interface DetailFieldProps {
  label: string;
  value: ReactNode;
  valueClassName?: string;
  /** "dt" (default) renders a dt/dd pair for use inside a <dl>; "span"
   * renders a span/p pair for use outside one (e.g. a plain stat grid). */
  as?: "dt" | "span";
}

// The label-above-value cell repeated across every detail grid (Trace
// Detail, Grade Detail, Phase Run Detail) — one place to keep the
// label/value styling consistent instead of re-declaring it per field.
export function DetailField({
  label,
  value,
  valueClassName = "text-gray-800 dark:text-gray-200",
  as = "dt",
}: DetailFieldProps) {
  if (as === "span") {
    return (
      <div>
        <span className="text-gray-600 dark:text-gray-500">{label}</span>
        <p className={valueClassName}>{value}</p>
      </div>
    );
  }
  return (
    <div>
      <dt className="text-gray-600 dark:text-gray-500">{label}</dt>
      <dd className={valueClassName}>{value}</dd>
    </div>
  );
}
