import { NextRequest, NextResponse } from "next/server";
import { isTrustedWriteContext } from "@/lib/write-guard";
import {
  decodeGradeListCursor,
  gradeListFiltersSchema,
  parseSearchParams,
} from "@/lib/feedback-grade-filters";
import { listGrades } from "@/lib/feedback-grade-queries";
import type { GradeListCursor } from "@/types/feedback-grades";

// Explorer rows carry post content, draft/critique text, and grade notes —
// reuse the same trusted-host boundary the feedback snapshot routes use.
export async function GET(request: NextRequest) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { errors: ["this endpoint requires a trusted network context"] },
      { status: 403 }
    );
  }

  const parsed = parseSearchParams(request.nextUrl.searchParams, gradeListFiltersSchema);
  if (!parsed.ok) {
    return NextResponse.json({ errors: parsed.errors }, { status: 400 });
  }
  const { cursor: cursorParam, ...rest } = parsed.data;

  let cursor: GradeListCursor | undefined;
  if (cursorParam !== undefined) {
    const decoded = decodeGradeListCursor(cursorParam);
    if (decoded === null) {
      return NextResponse.json({ errors: ["cursor: invalid cursor"] }, { status: 400 });
    }
    cursor = decoded;
  }

  const asOf = new Date().toISOString();
  const page = listGrades({ ...rest, cursor }, asOf);
  return NextResponse.json(page);
}
