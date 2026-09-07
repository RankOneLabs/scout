"use client";
import Link from "next/link";
import { useState } from "react";
import { useReviewQueues } from "@/hooks/use-review-queues";

export default function ReviewQueuesPage() {
  const { queues, error, loading } = useReviewQueues();
  const [project, setProject] = useState("");
  const projects = [...new Set(queues.map((queue) => queue.project_key))].sort();
  const visible = queues.filter((queue) => !project || queue.project_key === project);
  return <div className="space-y-4">
    <h1 className="text-2xl font-semibold">Grading-assistance queues</h1>
    <p className="text-sm text-gray-500">Frozen ranked and random selections. Review saves judgments only; no automatic drafting or retraining.</p>
    <label className="text-sm">Project <select value={project} onChange={(event) => setProject(event.target.value)} className="rounded border bg-white p-1 dark:bg-gray-900">
      <option value="">All projects</option>{projects.map((value) => <option key={value}>{value}</option>)}
    </select></label>
    {error && <p role="alert">{error}</p>}
    {loading && <p>Loading…</p>}
    {!loading && !error && visible.length === 0 && <p>No retained queues. Create one with scout analysis assistance-run.</p>}
    <ul className="divide-y divide-gray-300 dark:divide-gray-700">{visible.map((queue) => <li key={queue.digest} className="space-y-1 py-3">
      <Link href={`/feedback/review-queues/${queue.digest}`} className="text-blue-600 dark:text-blue-400">{queue.project_key} · {queue.digest.slice(0, 12)}</Link>
      <p className="text-sm">{queue.reviewed}/{queue.source_count} reviewed · {queue.skipped} skipped · {queue.graded_elsewhere} graded elsewhere</p>
    </li>)}</ul>
  </div>;
}
