import { NextRequest, NextResponse } from "next/server";
import { getGradingProgress } from "@/lib/queries";
import { parseIdParam } from "@/lib/route-utils";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const scanId = parseIdParam(id);
  if (scanId === null) {
    return NextResponse.json({ error: "invalid scan id" }, { status: 400 });
  }
  const progress = getGradingProgress(scanId);
  return NextResponse.json(progress);
}
