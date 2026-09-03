import { NextRequest, NextResponse } from "next/server";
import { isTrustedWriteContext } from "@/lib/write-guard";
import { parseIdParam } from "@/lib/route-utils";
import {
  decodePhaseRunCursor,
  decodeRevisionCursor,
  decodeSnapshotUseHistoryCursor,
  gradeDetailFiltersSchema,
  parseSearchParams,
} from "@/lib/feedback-grade-filters";
import { getGradeDetail } from "@/lib/feedback-grade-queries";
import type { GradeDetailPagingOptions } from "@/types/feedback-grades";

type RouteParams = { params: Promise<{ gradeId: string }> };

// grade_id is the canonical, stable audit address — survives null
// evaluation linkage and later adoption. Carries post content, draft/
// critique text, and revision payloads, so reuse the same trusted-host
// boundary the feedback snapshot routes use.
export async function GET(request: NextRequest, { params }: RouteParams) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { errors: ["this endpoint requires a trusted network context"] },
      { status: 403 }
    );
  }

  const gradeId = parseIdParam((await params).gradeId);
  if (gradeId === null) {
    return NextResponse.json({ errors: ["invalid grade id"] }, { status: 400 });
  }

  const parsed = parseSearchParams(request.nextUrl.searchParams, gradeDetailFiltersSchema);
  if (!parsed.ok) {
    return NextResponse.json({ errors: parsed.errors }, { status: 400 });
  }

  const paging: GradeDetailPagingOptions = {
    revisionLimit: parsed.data.revisionLimit,
    snapshotUseLimit: parsed.data.snapshotUseLimit,
    phaseRunLimit: parsed.data.phaseRunLimit,
  };

  if (parsed.data.revisionCursor !== undefined) {
    const decoded = decodeRevisionCursor(parsed.data.revisionCursor);
    if (decoded === null) {
      return NextResponse.json({ errors: ["revisionCursor: invalid cursor"] }, { status: 400 });
    }
    paging.revisionCursor = decoded;
  }
  if (parsed.data.snapshotUseCursor !== undefined) {
    const decoded = decodeSnapshotUseHistoryCursor(parsed.data.snapshotUseCursor);
    if (decoded === null) {
      return NextResponse.json({ errors: ["snapshotUseCursor: invalid cursor"] }, { status: 400 });
    }
    paging.snapshotUseCursor = decoded;
  }
  if (parsed.data.phaseRunCursor !== undefined) {
    const decoded = decodePhaseRunCursor(parsed.data.phaseRunCursor);
    if (decoded === null) {
      return NextResponse.json({ errors: ["phaseRunCursor: invalid cursor"] }, { status: 400 });
    }
    paging.phaseRunCursor = decoded;
  }

  const asOf = new Date().toISOString();
  const detail = getGradeDetail(gradeId, asOf, paging);
  if (detail === null) {
    return NextResponse.json({ errors: ["grade not found"] }, { status: 404 });
  }

  return NextResponse.json(detail);
}
