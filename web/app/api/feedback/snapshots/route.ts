import { NextRequest, NextResponse } from "next/server";
import { isTrustedWriteContext } from "@/lib/write-guard";
import {
  decodeSnapshotListCursor,
  parseSearchParams,
  snapshotListFiltersSchema,
} from "@/lib/feedback-filters";
import { listFeedbackSnapshots } from "@/lib/feedback-queries";

// Feedback snapshots carry prompt-relevant grade notes and revision
// payloads — reuse the same trusted-host boundary the write routes use,
// even though this route is otherwise read-only.
export async function GET(request: NextRequest) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { errors: ["this endpoint requires a trusted network context"] },
      { status: 403 }
    );
  }

  const parsed = parseSearchParams(request.nextUrl.searchParams, snapshotListFiltersSchema);
  if (!parsed.ok) {
    return NextResponse.json({ errors: parsed.errors }, { status: 400 });
  }
  const { scanId, mode, limit, cursor: cursorParam } = parsed.data;

  let cursor;
  if (cursorParam !== undefined) {
    const decoded = decodeSnapshotListCursor(cursorParam);
    if (decoded === null) {
      return NextResponse.json({ errors: ["cursor: invalid cursor"] }, { status: 400 });
    }
    cursor = decoded;
  }

  const page = listFeedbackSnapshots({ scanId, mode, limit, cursor });
  return NextResponse.json(page);
}
