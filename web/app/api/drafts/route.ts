import { NextRequest, NextResponse } from "next/server";
import { getDrafts, getDraftsWithGrades } from "@/lib/queries";
import { parseSearchParams, draftFiltersSchema } from "@/lib/filter-schemas";

export async function GET(request: NextRequest) {
  const parsed = parseSearchParams(
    request.nextUrl.searchParams,
    draftFiltersSchema
  );
  if (!parsed.ok) {
    return NextResponse.json({ errors: parsed.errors }, { status: 400 });
  }
  const { include_grades, ...filters } = parsed.data;
  const result = include_grades
    ? getDraftsWithGrades(filters)
    : getDrafts(filters);
  return NextResponse.json(result);
}
