import { notFound } from "next/navigation";
import { PhaseRunDetail } from "@/components/organisms/PhaseRunDetail";
import { parseIdParam } from "@/lib/route-utils";

type PageParams = { params: Promise<{ phaseRunId: string }> };

export default async function PhaseRunDetailPage({ params }: PageParams) {
  const phaseRunId = parseIdParam((await params).phaseRunId);
  if (phaseRunId === null) notFound();
  return <PhaseRunDetail phaseRunId={phaseRunId} />;
}
