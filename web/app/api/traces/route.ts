import { NextRequest, NextResponse } from "next/server";
import { getTraces } from "@/lib/trace-queries";

export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;
  const errorOnly = searchParams.get("error_only") === "true";
  const limitParam = searchParams.get("limit");
  let limit: number | undefined;
  if (limitParam !== null) {
    const parsed = Number(limitParam);
    if (Number.isFinite(parsed) && parsed > 0) {
      limit = Math.min(Math.floor(parsed), 1000);
    }
  }

  const traces = getTraces({ error_only: errorOnly, limit });
  return NextResponse.json(traces);
}
