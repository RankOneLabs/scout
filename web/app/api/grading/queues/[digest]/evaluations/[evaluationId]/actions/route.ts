import { NextRequest, NextResponse } from "next/server";
import { callSidecar, parseObjectBody } from "@/lib/sidecar-bridge";
import { isTrustedWriteContext } from "@/lib/write-guard";
import { parseIdParam } from "@/lib/route-utils";
import { digestSchema } from "@/types/review-queues";

export async function POST(request: NextRequest, { params }: { params: Promise<{ digest: string; evaluationId: string }> }) {
  if (!isTrustedWriteContext(request)) return NextResponse.json({ detail: "Untrusted write context" }, { status: 403 });
  const values = await params;
  const digest = digestSchema.safeParse(values.digest);
  const evaluationId = parseIdParam(values.evaluationId);
  if (!digest.success || evaluationId === null) return NextResponse.json({ detail: "Invalid queue or evaluation" }, { status: 400 });
  const parsed = await parseObjectBody(request);
  if (!parsed.ok) return NextResponse.json({ detail: "Invalid JSON body" }, { status: 400 });
  const result = await callSidecar(`/review-queues/${digest.data}/evaluations/${evaluationId}/actions`, parsed.body);
  return NextResponse.json(result.body, { status: result.status });
}
