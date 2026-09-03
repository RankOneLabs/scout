import { notFound } from "next/navigation";
import { GradeDetail } from "@/components/organisms/GradeDetail";
import { parseIdParam } from "@/lib/route-utils";

type PageParams = { params: Promise<{ gradeId: string }> };

export default async function GradeDetailPage({ params }: PageParams) {
  const gradeId = parseIdParam((await params).gradeId);
  if (gradeId === null) notFound();
  return <GradeDetail gradeId={gradeId} />;
}
