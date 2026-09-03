import { NextRequest, NextResponse } from "next/server";
import { getScans, getScanById } from "@/lib/queries";
import { getFeedbackSnapshotSummaryForScan } from "@/lib/feedback-queries";
import { parseIdParam } from "@/lib/route-utils";

export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;
  const idParam = searchParams.get("id");

  if (idParam !== null) {
    const id = parseIdParam(idParam);
    if (id === null) {
      return NextResponse.json({ error: "invalid scan id" }, { status: 400 });
    }
    const scan = getScanById(id);
    if (!scan) {
      return NextResponse.json({ error: "Scan not found" }, { status: 404 });
    }
    return NextResponse.json({ ...scan, feedback: getFeedbackSnapshotSummaryForScan(id) });
  }

  const scans = getScans();
  return NextResponse.json(scans);
}
