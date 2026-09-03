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
import { getGradeDetailByEvaluationId } from "@/lib/feedback-grade-queries";
import type { GradeDetailPagingOptions } from "@/types/feedback-grades";

type RouteParams = { params: Promise<{ evaluationId: string }> };

// Compatibility address for the original evaluation-scoped read contract.
// The canonical explorer route remains grade_id-based so unlinked grades are
// still addressable, while existing evaluation-oriented callers can resolve
// the same current authoritative grade detail without ambiguity.
export async function GET(request: NextRequest, { params }: RouteParams) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { errors: ["this endpoint requires a trusted network context"] },
      { status: 403 }
    );
  }

  const evaluationId = parseIdParam((await params).evaluationId);
  if (evaluationId === null) {
    return NextResponse.json({ errors: ["invalid evaluation id"] }, { status: 400 });
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
    const cursor = decodeRevisionCursor(parsed.data.revisionCursor);
    if (cursor === null) {
      return NextResponse.json({ errors: ["revisionCursor: invalid cursor"] }, { status: 400 });
    }
    paging.revisionCursor = cursor;
  }
  if (parsed.data.snapshotUseCursor !== undefined) {
    const cursor = decodeSnapshotUseHistoryCursor(parsed.data.snapshotUseCursor);
    if (cursor === null) {
      return NextResponse.json(
        { errors: ["snapshotUseCursor: invalid cursor"] },
        { status: 400 }
      );
    }
    paging.snapshotUseCursor = cursor;
  }
  if (parsed.data.phaseRunCursor !== undefined) {
    const cursor = decodePhaseRunCursor(parsed.data.phaseRunCursor);
    if (cursor === null) {
      return NextResponse.json({ errors: ["phaseRunCursor: invalid cursor"] }, { status: 400 });
    }
    paging.phaseRunCursor = cursor;
  }

  const detail = getGradeDetailByEvaluationId(
    evaluationId,
    new Date().toISOString(),
    paging
  );
  if (detail === null) {
    return NextResponse.json({ errors: ["grade not found"] }, { status: 404 });
  }
  return NextResponse.json(detail);
}
