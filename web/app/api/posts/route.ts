import { NextRequest, NextResponse } from "next/server";
import { getPosts } from "@/lib/queries";
import { parseSearchParams, postFiltersSchema } from "@/lib/filter-schemas";

export async function GET(request: NextRequest) {
  const parsed = parseSearchParams(
    request.nextUrl.searchParams,
    postFiltersSchema
  );
  if (!parsed.ok) {
    return NextResponse.json({ errors: parsed.errors }, { status: 400 });
  }
  const result = getPosts(parsed.data);
  return NextResponse.json(result);
}
