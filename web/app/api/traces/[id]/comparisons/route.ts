import { NextRequest, NextResponse } from "next/server";
import { isTrustedWriteContext } from "@/lib/write-guard";
import { getTraceComparisonBacklinks } from "@/lib/trace-queries";

type RouteParams = { params: Promise<{ id: string }> };

// Trace Detail's second backlink relationship set: every replay comparison
// that used this trace as baseline or candidate, independent of the
// existing single phase-run backlink at /traces/[id]/phase-run. Always
// 200s with a (possibly empty) array — "no comparisons" is a well-formed
// answer for most traces, not a 404. Same trusted-host boundary as the
// phase-run backlink route.
export async function GET(request: NextRequest, { params }: RouteParams) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { errors: ["this endpoint requires a trusted network context"] },
      { status: 403 }
    );
  }

  const { id } = await params;
  const backlinks = getTraceComparisonBacklinks(id);
  return NextResponse.json(backlinks);
}
