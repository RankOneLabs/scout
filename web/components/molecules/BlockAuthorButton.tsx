"use client";

import { useState } from "react";

interface BlockAuthorButtonProps {
  platform: string;
  authorId: string | null | undefined;
  authorName: string | null | undefined;
}

export function BlockAuthorButton({
  platform,
  authorId,
  authorName,
}: BlockAuthorButtonProps) {
  const [status, setStatus] = useState<"idle" | "saving" | "blocked">("idle");
  const [error, setError] = useState<string | null>(null);

  if (!authorId) return null;

  const block = async () => {
    setStatus("saving");
    setError(null);
    try {
      const response = await fetch("/api/settings/blocked-authors", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          platform,
          author_id: authorId,
          author_name: authorName ?? null,
          reason: "blocked from Scout review UI",
        }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setStatus("blocked");
    } catch (cause) {
      setStatus("idle");
      setError(cause instanceof Error ? cause.message : "block failed");
    }
  };

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        onClick={block}
        disabled={status !== "idle"}
        className="text-xs text-red-600 hover:text-red-500 disabled:cursor-not-allowed disabled:text-gray-400 dark:text-red-400 dark:hover:text-red-300 dark:disabled:text-gray-600"
      >
        {status === "saving"
          ? "Blocking…"
          : status === "blocked"
            ? "Author blocked"
            : "Block author"}
      </button>
      {error && <span className="text-xs text-red-600 dark:text-red-400">{error}</span>}
    </div>
  );
}
