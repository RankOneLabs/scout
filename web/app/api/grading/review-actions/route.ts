import { NextRequest, NextResponse } from "next/server";
import { listEvaluationReviews } from "@/lib/review-queue-queries";
import { parseIdParam } from "@/lib/route-utils";

export function GET(request: NextRequest) {
  const evaluationId = parseIdParam(request.nextUrl.searchParams.get("evaluationId") ?? "");
  if (evaluationId === null) return NextResponse.json({ detail: "Invalid evaluation ID" }, { status: 400 });
  const result = listEvaluationReviews(evaluationId);
  return result.ok ? NextResponse.json(result.value) : NextResponse.json({ detail: result.error }, { status: 503 });
}
