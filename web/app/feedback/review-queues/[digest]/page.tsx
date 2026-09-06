import { ReviewQueueWorkspace } from "@/components/organisms/ReviewQueueWorkspace";
import { parseIdParam } from "@/lib/route-utils";

export default async function ReviewQueuePage({ params, searchParams }: { params: Promise<{ digest: string }>; searchParams: Promise<{ evaluationId?: string }> }) {
  const { digest } = await params;
  const evaluationId = parseIdParam((await searchParams).evaluationId ?? "");
  return <ReviewQueueWorkspace key={digest} digest={digest} initialEvaluationId={evaluationId} />;
}
