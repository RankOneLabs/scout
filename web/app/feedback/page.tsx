"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useFeedbackSnapshots } from "@/hooks/use-feedback-snapshots";
import { FeedbackWorkspace } from "@/components/organisms/FeedbackWorkspace";
import { parseIdParam } from "@/lib/route-utils";

export default function FeedbackPage() {
  return (
    <Suspense
      fallback={
        <div className="flex h-64 items-center justify-center">
          <p className="text-sm text-gray-600 dark:text-gray-500">Loading...</p>
        </div>
      }
    >
      <FeedbackPageContent />
    </Suspense>
  );
}

// snapshotId is the canonical shareable URL parameter. Arriving with only
// scanId (e.g. from a Scan detail link) resolves that scan's snapshot and
// redirects to the canonical form; arriving with neither resolves to the
// newest snapshot overall.
function FeedbackPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const snapshotIdParam = searchParams.get("snapshotId");
  const scanIdParam = searchParams.get("scanId");

  const history = useFeedbackSnapshots({});
  const [scanResolveError, setScanResolveError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    // Clear any error left over from a previous scanId (or from a previous
    // run of this same effect) so navigating away from a failed resolution
    // doesn't leave a stale message on screen.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setScanResolveError(null);

    if (snapshotIdParam !== null) return;

    if (scanIdParam !== null) {
      const scanId = parseIdParam(scanIdParam);
      if (scanId === null) {
        setScanResolveError("invalid scan id");
        return;
      }
      fetch(`/api/feedback/snapshots?scanId=${scanId}&limit=1`)
        .then((res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return res.json() as Promise<{ data: { snapshot_id: number }[] }>;
        })
        .then((page) => {
          if (cancelled) return;
          if (page.data.length === 0) {
            setScanResolveError(`no feedback snapshot for scan ${scanId}`);
            return;
          }
          router.replace(`/feedback?snapshotId=${page.data[0].snapshot_id}`);
        })
        .catch((err: unknown) => {
          if (cancelled) return;
          setScanResolveError(err instanceof Error ? err.message : String(err));
        });
      return () => {
        cancelled = true;
      };
    }

    if (!history.loading && history.data.length > 0) {
      router.replace(`/feedback?snapshotId=${history.data[0].snapshot_id}`);
    }

    return () => {
      cancelled = true;
    };
  }, [snapshotIdParam, scanIdParam, history.loading, history.data, router]);

  if (snapshotIdParam === null) {
    if (scanResolveError) {
      return (
        <div className="flex h-64 items-center justify-center">
          <p className="text-sm text-gray-600 dark:text-gray-500">
            Feedback snapshot unavailable: {scanResolveError}
          </p>
        </div>
      );
    }
    if (!history.loading && history.data.length === 0 && scanIdParam === null) {
      return (
        <div className="flex h-64 items-center justify-center">
          <p className="text-sm text-gray-600 dark:text-gray-500">No feedback snapshots recorded yet.</p>
        </div>
      );
    }
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">Loading...</p>
      </div>
    );
  }

  const snapshotId = parseIdParam(snapshotIdParam);
  if (snapshotId === null) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">Invalid snapshot id.</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Feedback</h1>
      <FeedbackWorkspace
        snapshotId={snapshotId}
        history={history.data}
        historyLoading={history.loading}
        historyError={history.error}
      />
    </div>
  );
}
