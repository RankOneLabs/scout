import { NextRequest, NextResponse } from "next/server";
import { getEvaluationsByScan } from "@/lib/queries";
import {
  parseSearchParams,
  evaluationFiltersSchema,
} from "@/lib/filter-schemas";

export async function GET(request: NextRequest) {
  const parsed = parseSearchParams(
    request.nextUrl.searchParams,
    evaluationFiltersSchema
  );
  if (!parsed.ok) {
    return NextResponse.json({ errors: parsed.errors }, { status: 400 });
  }

  const evaluations = getEvaluationsByScan(parsed.data.scan_id);
  return NextResponse.json(evaluations);
}
