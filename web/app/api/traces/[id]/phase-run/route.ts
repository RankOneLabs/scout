import { NextRequest, NextResponse } from "next/server";
import { isTrustedWriteContext } from "@/lib/write-guard";
import { getPhaseRunByTraceId } from "@/lib/feedback-phase-run-queries";

type RouteParams = { params: Promise<{ id: string }> };

// Trace Detail's backlink to phase run, linked evaluation, and snapshot
// phase — resolved by joining only evaluation_phase_runs.trace_id, the one
// approved unambiguous backlink (never prompt hash, post identity, model,
// or time proximity). Same trusted-host boundary as the phase-runs route,
// since a hit surfaces the same scan/post/evaluation linkage.
export async function GET(request: NextRequest, { params }: RouteParams) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { errors: ["this endpoint requires a trusted network context"] },
      { status: 403 }
    );
  }

  const { id } = await params;
  const detail = getPhaseRunByTraceId(id);
  if (detail === null) {
    return NextResponse.json({ errors: ["no linked phase run for this trace"] }, { status: 404 });
  }

  return NextResponse.json(detail);
}
