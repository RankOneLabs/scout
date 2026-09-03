import { notFound } from "next/navigation";
import { ExperimentRunDetail } from "@/components/organisms/ExperimentRunDetail";
import { parseIdParam } from "@/lib/route-utils";

type PageParams = { params: Promise<{ experimentRunId: string }> };
export default async function Page({ params }: PageParams) {
  const experimentRunId = parseIdParam((await params).experimentRunId);
  if (experimentRunId === null) notFound();
  return <ExperimentRunDetail experimentRunId={experimentRunId} />;
}
