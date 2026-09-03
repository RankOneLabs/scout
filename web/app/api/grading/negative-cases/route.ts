import { NextRequest, NextResponse } from "next/server";
import { negativeGradingFiltersSchema, parseSearchParams } from "@/lib/filter-schemas";
import { getNegativeGradingCases } from "@/lib/queries";

export async function GET(request: NextRequest) {
  const parsed = parseSearchParams(
    request.nextUrl.searchParams,
    negativeGradingFiltersSchema
  );
  if (!parsed.ok) {
    return NextResponse.json({ errors: parsed.errors }, { status: 400 });
  }
  return NextResponse.json(getNegativeGradingCases(parsed.data));
}
