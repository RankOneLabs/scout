import { notFound } from "next/navigation";
import { ExperimentDetail } from "@/components/organisms/ExperimentDetail";
import { parseIdParam } from "@/lib/route-utils";

type PageParams = { params: Promise<{ experimentId: string }> };

export default async function ExperimentDetailPage({ params }: PageParams) {
  const experimentId = parseIdParam((await params).experimentId);
  if (experimentId === null) notFound();
  return <ExperimentDetail experimentId={experimentId} />;
}
