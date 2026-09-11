import type { AuthorClassification } from "@/types/schema";

interface AuthorClassBadgeProps {
  classification: AuthorClassification | null | undefined;
}

/**
 * Advisory account class from the scan runner's annotate node. Only a
 * positive class is shown: "unknown" is the absence of a signal, not a
 * finding, and would only add noise beside the block button.
 */
export function AuthorClassBadge({ classification }: AuthorClassBadgeProps) {
  if (!classification || classification.author_class === "unknown") return null;
  const detail = classification.matched_text
    ? `matched "${classification.matched_text}" (rule v${classification.rule_version})`
    : `rule v${classification.rule_version}`;
  const explanation = `Name or handle looks like an automated feed: ${detail}. Advisory only; the post was still evaluated.`;
  return (
    <span
      title={explanation}
      className="rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide text-amber-800 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-300"
    >
      {classification.author_class}
      <span className="sr-only">. {explanation}</span>
    </span>
  );
}
