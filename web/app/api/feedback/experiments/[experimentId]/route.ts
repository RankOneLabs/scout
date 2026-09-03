import { NextRequest, NextResponse } from "next/server";
import { isTrustedWriteContext } from "@/lib/write-guard";
import { parseIdParam } from "@/lib/route-utils";
import { DataIntegrityError, getExperimentDetail } from "@/lib/feedback-experiment-queries";

type RouteParams = { params: Promise<{ experimentId: string }> };

// Stored-ID detail for one replay experiment: baseline phase run/trace,
// snapshot/policy inputs, candidate config, and the persisted comparison.
// Same trusted-host boundary as the list route — this response embeds
// both prompts.
export async function GET(request: NextRequest, { params }: RouteParams) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { errors: ["this endpoint requires a trusted network context"] },
      { status: 403 }
    );
  }

  const experimentId = parseIdParam((await params).experimentId);
  if (experimentId === null) {
    return NextResponse.json({ errors: ["invalid experiment id"] }, { status: 400 });
  }

  let detail;
  try {
    detail = getExperimentDetail(experimentId);
  } catch (err) {
    if (err instanceof DataIntegrityError) {
      return NextResponse.json({ errors: ["internal data-integrity error"] }, { status: 500 });
    }
    throw err;
  }
  if (detail === null) {
    return NextResponse.json({ errors: ["experiment not found"] }, { status: 404 });
  }

  return NextResponse.json(detail);
}
