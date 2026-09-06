import { NextRequest, NextResponse } from "next/server";
import { listReviewQueues } from "@/lib/review-queue-queries";

export function GET(request: NextRequest) {
  const result = listReviewQueues(request.nextUrl.searchParams.get("project"));
  return result.ok ? NextResponse.json(result.value) : NextResponse.json({ detail: result.error }, { status: 503 });
}
