import { NextRequest, NextResponse } from "next/server";
import { isTrustedWriteContext } from "@/lib/write-guard";
import { parseIdParam } from "@/lib/route-utils";
import {
  decodeFeedbackItemCursor,
  parseSearchParams,
  snapshotDetailFiltersSchema,
} from "@/lib/feedback-filters";
import { getFeedbackSnapshotDetail } from "@/lib/feedback-queries";
import { decodePhaseRunCursor } from "@/lib/feedback-phase-run-filters";

type RouteParams = { params: Promise<{ snapshotId: string }> };

// Feedback snapshots carry prompt-relevant grade notes and revision
// payloads — reuse the same trusted-host boundary the write routes use,
// even though this route is otherwise read-only.
export async function GET(request: NextRequest, { params }: RouteParams) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { errors: ["this endpoint requires a trusted network context"] },
      { status: 403 }
    );
  }

  const snapshotId = parseIdParam((await params).snapshotId);
  if (snapshotId === null) {
    return NextResponse.json({ errors: ["invalid snapshot id"] }, { status: 400 });
  }

  const parsed = parseSearchParams(request.nextUrl.searchParams, snapshotDetailFiltersSchema);
  if (!parsed.ok) {
    return NextResponse.json({ errors: parsed.errors }, { status: 400 });
  }
  const {
    phase,
    use,
    itemLimit,
    itemCursor: itemCursorParam,
    consumerLimit,
    consumerCursor: consumerCursorParam,
  } = parsed.data;

  let itemCursor;
  if (itemCursorParam !== undefined) {
    const decoded = decodeFeedbackItemCursor(itemCursorParam);
    if (decoded === null) {
      return NextResponse.json({ errors: ["itemCursor: invalid cursor"] }, { status: 400 });
    }
    itemCursor = decoded;
  }

  let consumerCursor;
  if (consumerCursorParam !== undefined) {
    const decoded = decodePhaseRunCursor(consumerCursorParam);
    if (decoded === null) {
      return NextResponse.json({ errors: ["consumerCursor: invalid cursor"] }, { status: 400 });
    }
    consumerCursor = decoded;
  }

  const detail = getFeedbackSnapshotDetail(snapshotId, {
    phase,
    use,
    itemLimit,
    itemCursor,
    consumerLimit,
    consumerCursor,
  });
  if (detail === null) {
    return NextResponse.json({ errors: ["snapshot not found"] }, { status: 404 });
  }

  return NextResponse.json(detail);
}
