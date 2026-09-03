"use client";

import type { PostWithEvaluation, SourceParent } from "@/types/schema";
import { Badge } from "@/components/atoms/Badge";
import { StatusDot } from "@/components/atoms/StatusDot";
import { ScoreBar } from "@/components/atoms/ScoreBar";
import { ExternalLink } from "@/components/atoms/ExternalLink";
import { MatchedRouteSummary } from "@/components/molecules/MatchedRouteSummary";
import { truncateContent } from "@/lib/transforms";

interface PostPreviewProps {
  post: PostWithEvaluation;
}

function ParentQuote({ parent }: { parent: SourceParent }) {
  return (
    <div className="mt-2 border-l-2 border-gray-300 dark:border-gray-700 pl-3">
      <span className="text-xs text-gray-600 dark:text-gray-500">
        replying to {parent.author.name}
      </span>
      <p className="mt-0.5 text-xs text-gray-600 dark:text-gray-500 italic">
        {truncateContent(parent.text, 120)}
      </p>
      {parent.url && (
        <ExternalLink href={parent.url}>source</ExternalLink>
      )}
    </div>
  );
}

export function PostPreview({ post }: PostPreviewProps) {
  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 p-4">
      {post.parent_lookup_status === "resolved" && post.parent && (
        <ParentQuote parent={post.parent} />
      )}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-wrap items-center gap-2">
          <StatusDot platform={post.platform} />
          <Badge label={post.platform} variant="platform" />
          <span className="text-sm text-gray-600 dark:text-gray-400">
            {post.author_name ?? "unknown"}
          </span>
          {post.channel_name && (
            <span className="text-xs text-gray-500 dark:text-gray-400">#{post.channel_name}</span>
          )}
        </div>
        <div className="flex items-center gap-3">
          {post.score !== null && <ScoreBar score={post.score} />}
          {post.url && <ExternalLink href={post.url}>View</ExternalLink>}
        </div>
      </div>
      <p className="mt-2 text-sm text-gray-700 dark:text-gray-300">
        {post.content ? truncateContent(post.content, 200) : "—"}
      </p>
      {post.relevant_to.length > 0 && (
        <div className="mt-2 flex gap-1">
          {post.relevant_to.map((project) => (
            <Badge key={project} label={project} variant="project" />
          ))}
        </div>
      )}
      <MatchedRouteSummary route={post.matched_route} />
    </div>
  );
}
