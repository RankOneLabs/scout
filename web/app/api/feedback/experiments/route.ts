import { NextRequest, NextResponse } from "next/server";
import { isTrustedWriteContext } from "@/lib/write-guard";
import {
  cursorMatchesFilters,
  decodeExperimentListCursor,
  parseExperimentListSearchParams,
} from "@/lib/feedback-experiment-filters";
import { DataIntegrityError, listExperiments } from "@/lib/feedback-experiment-queries";
import type { ExperimentListCursor } from "@/types/feedback-experiments";

// Experiment rows carry prompts and structured model output — same
// trusted-host boundary as the other feedback read routes. This route is
// inspection-only: it never opens a write connection or touches replay.
export async function GET(request: NextRequest) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { errors: ["this endpoint requires a trusted network context"] },
      { status: 403 }
    );
  }

  const parsed = parseExperimentListSearchParams(request.nextUrl.searchParams);
  if (!parsed.ok) {
    return NextResponse.json({ errors: parsed.errors }, { status: 400 });
  }
  const { cursor: cursorParam, status, phase, limit } = parsed.data;

  let cursor: ExperimentListCursor | undefined;
  if (cursorParam !== undefined) {
    const decoded = decodeExperimentListCursor(cursorParam);
    if (decoded === null) {
      return NextResponse.json({ errors: ["cursor: invalid cursor"] }, { status: 400 });
    }
    if (!cursorMatchesFilters(decoded, { status, phase })) {
      return NextResponse.json(
        { errors: ["cursor: does not match the current status/phase filters"] },
        { status: 400 }
      );
    }
    cursor = decoded;
  }

  let page;
  try {
    page = listExperiments({ status, phase, limit, cursor });
  } catch (err) {
    if (err instanceof DataIntegrityError) {
      return NextResponse.json({ errors: ["internal data-integrity error"] }, { status: 500 });
    }
    throw err;
  }
  return NextResponse.json(page);
}
