import { NextRequest, NextResponse } from "next/server";
import { getReviewQueue } from "@/lib/review-queue-queries";
import { digestSchema } from "@/types/review-queues";

export async function GET(_request: NextRequest, { params }: { params: Promise<{ digest: string }> }) {
  const parsed = digestSchema.safeParse((await params).digest);
  if (!parsed.success) return NextResponse.json({ detail: "Invalid queue digest" }, { status: 400 });
  const result = getReviewQueue(parsed.data);
  return result.ok ? NextResponse.json(result.value) : NextResponse.json({ detail: result.error }, { status: 503 });
}
