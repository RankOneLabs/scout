import { NextRequest, NextResponse } from "next/server";
import { getTraceSpans } from "@/lib/trace-queries";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const spans = getTraceSpans(id);

  if (spans.length === 0) {
    return NextResponse.json({ error: "Trace not found" }, { status: 404 });
  }

  return NextResponse.json(spans);
}
