import { NextRequest, NextResponse } from "next/server";
import { isTrustedWriteContext } from "@/lib/write-guard";
import { parseIdParam } from "@/lib/route-utils";
import { getPhaseRunDetail } from "@/lib/feedback-phase-run-queries";

type RouteParams = { params: Promise<{ phaseRunId: string }> };

// Stored-ID-only identity for one relevance/reply_draft/critic phase
// attempt: its scan/post/evaluation/snapshot-phase links and its Jig
// trace_id, never prompt content or grade evidence. Same trusted-host
// boundary as the grade detail routes, since this surfaces the same class
// of scan/post-linked evidence.
export async function GET(request: NextRequest, { params }: RouteParams) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { errors: ["this endpoint requires a trusted network context"] },
      { status: 403 }
    );
  }

  const phaseRunId = parseIdParam((await params).phaseRunId);
  if (phaseRunId === null) {
    return NextResponse.json({ errors: ["invalid phase run id"] }, { status: 400 });
  }

  const detail = getPhaseRunDetail(phaseRunId);
  if (detail === null) {
    return NextResponse.json({ errors: ["phase run not found"] }, { status: 404 });
  }

  return NextResponse.json(detail);
}
