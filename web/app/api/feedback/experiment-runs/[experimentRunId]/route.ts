import { NextRequest, NextResponse } from "next/server";
import { isTrustedWriteContext } from "@/lib/write-guard";
import { parseIdParam } from "@/lib/route-utils";
import { DataIntegrityError, getExperimentRunDetail } from "@/lib/feedback-experiment-queries";

type RouteParams = { params: Promise<{ experimentRunId: string }> };
const NO_STORE = { "Cache-Control": "no-store" };

export async function GET(request: NextRequest, { params }: RouteParams) {
  if (!isTrustedWriteContext(request)) return NextResponse.json({ errors: ["this endpoint requires a trusted network context"] }, { status: 403, headers: NO_STORE });
  const id = parseIdParam((await params).experimentRunId);
  if (id === null) return NextResponse.json({ errors: ["invalid experiment run id"] }, { status: 400, headers: NO_STORE });
  try {
    const detail = getExperimentRunDetail(id);
    return detail === null
      ? NextResponse.json({ errors: ["experiment run not found"] }, { status: 404, headers: NO_STORE })
      : NextResponse.json(detail, { headers: NO_STORE });
  } catch (error) {
    if (error instanceof DataIntegrityError) return NextResponse.json({ errors: ["internal data-integrity error"] }, { status: 500, headers: NO_STORE });
    throw error;
  }
}
