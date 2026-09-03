import { NextRequest, NextResponse } from "next/server";
import { getGateBlocksByScan, getGateBlocksByProject } from "@/lib/queries";
import { z } from "zod";
import { parseSearchParams } from "@/lib/filter-schemas";

const filtersSchema = z.object({
  scan_id: z.coerce.number().int().positive().optional(),
  project_key: z.string().min(1).optional(),
  limit: z.coerce.number().int().positive().max(500).optional().default(100),
});

export async function GET(request: NextRequest) {
  const parsed = parseSearchParams(request.nextUrl.searchParams, filtersSchema);
  if (!parsed.ok) {
    return NextResponse.json({ errors: parsed.errors }, { status: 400 });
  }
  const { scan_id, project_key, limit } = parsed.data;
  if (scan_id !== undefined) {
    return NextResponse.json({ gate_blocks: getGateBlocksByScan(scan_id, limit) });
  }
  if (project_key !== undefined) {
    return NextResponse.json({ gate_blocks: getGateBlocksByProject(project_key, limit) });
  }
  return NextResponse.json(
    { error: "scan_id or project_key is required" },
    { status: 400 }
  );
}
