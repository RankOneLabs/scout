interface GradingProgressProps {
  graded: number;
  total: number;
}

export function GradingProgress({ graded, total }: GradingProgressProps) {
  return (
    <span className="text-sm text-gray-600 dark:text-gray-400">
      Graded {graded} / {total} items
    </span>
  );
}
