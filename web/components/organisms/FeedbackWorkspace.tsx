"use client";

import { FEEDBACK_PHASES } from "@/types/feedback";
import type { SnapshotListItem } from "@/types/feedback";
import { useFeedbackSnapshotDetail } from "@/hooks/use-feedback-snapshot-detail";
import { FeedbackSnapshotHeaderCard } from "@/components/organisms/FeedbackSnapshotHeaderCard";
import { FeedbackSnapshotHistory } from "@/components/organisms/FeedbackSnapshotHistory";
import { FeedbackPhasePanel } from "@/components/organisms/FeedbackPhasePanel";

interface FeedbackWorkspaceProps {
  snapshotId: number;
  history: SnapshotListItem[];
  historyLoading: boolean;
  historyError: string | null;
}

export function FeedbackWorkspace({
  snapshotId,
  history,
  historyLoading,
  historyError,
}: FeedbackWorkspaceProps) {
  const {
    snapshot,
    phases,
    items,
    consumers,
    loading,
    error,
    notFound,
    loadMore,
    loadMoreConsumers,
    setUseFilter,
  } = useFeedbackSnapshotDetail(snapshotId);

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">Loading...</p>
      </div>
    );
  }

  if (notFound) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">Feedback snapshot not found.</p>
      </div>
    );
  }

  if (error || !snapshot || !phases) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">
          {error ? `Failed to load snapshot: ${error}` : "Feedback snapshot unavailable."}
        </p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
      <div className="space-y-4 lg:col-span-1">
        <FeedbackSnapshotHistory
          snapshots={history}
          loading={historyLoading}
          error={historyError}
          currentSnapshotId={snapshotId}
        />
      </div>
      <div className="space-y-4 lg:col-span-3">
        <FeedbackSnapshotHeaderCard snapshot={snapshot} />
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          {FEEDBACK_PHASES.map((phase) => (
            <FeedbackPhasePanel
              key={phase}
              summary={phases[phase]}
              mode={snapshot.mode}
              itemsState={items[phase]}
              consumersState={consumers[phase]}
              onUseFilterChange={(use) => setUseFilter(phase, use)}
              onLoadMore={() => loadMore(phase)}
              onLoadMoreConsumers={() => loadMoreConsumers(phase)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
