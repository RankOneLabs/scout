import { NextRequest, NextResponse } from "next/server";
import { isTrustedWriteContext } from "@/lib/write-guard";
import {
  decodeExperimentRunCursor,
  parseExperimentRunListSearchParams,
  runCursorMatchesFilters,
} from "@/lib/feedback-experiment-filters";
import { DataIntegrityError, listExperimentRuns } from "@/lib/feedback-experiment-queries";

const NO_STORE = { "Cache-Control": "no-store" };

export async function GET(request: NextRequest) {
  if (!isTrustedWriteContext(request)) return NextResponse.json({ errors: ["this endpoint requires a trusted network context"] }, { status: 403, headers: NO_STORE });
  const parsed = parseExperimentRunListSearchParams(request.nextUrl.searchParams);
  if (!parsed.ok) return NextResponse.json({ errors: parsed.errors }, { status: 400, headers: NO_STORE });
  const { cursor: rawCursor, ...filters } = parsed.data;
  const cursor = rawCursor === undefined ? undefined : decodeExperimentRunCursor(rawCursor);
  if (rawCursor !== undefined && cursor === null) return NextResponse.json({ errors: ["cursor: invalid cursor"] }, { status: 400, headers: NO_STORE });
  if (cursor && !runCursorMatchesFilters(cursor, filters)) return NextResponse.json({ errors: ["cursor: does not match the current filters"] }, { status: 400, headers: NO_STORE });
  try {
    return NextResponse.json(listExperimentRuns({ ...filters, cursor: cursor ?? undefined }), { headers: NO_STORE });
  } catch (error) {
    if (error instanceof DataIntegrityError) return NextResponse.json({ errors: ["internal data-integrity error"] }, { status: 500, headers: NO_STORE });
    throw error;
  }
}
