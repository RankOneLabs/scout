import { NextRequest, NextResponse } from "next/server";
import { callSidecar, parseObjectBody } from "@/lib/sidecar-bridge";
import { parseIdParam } from "@/lib/route-utils";
import { isTrustedWriteContext } from "@/lib/write-guard";
import { isEvaluationHeld } from "@/lib/queries";

type RouteParams = { params: Promise<{ evaluationId: string }> };

export async function POST(request: NextRequest, { params }: RouteParams) {
  if (!isTrustedWriteContext(request)) return NextResponse.json({ errors: ["write operations require a trusted network context"] }, { status: 403 });
  const evaluationId = parseIdParam((await params).evaluationId);
  if (evaluationId === null) return NextResponse.json({ errors: ["invalid evaluation id"] }, { status: 400 });
  if (isEvaluationHeld(evaluationId)) {
    return NextResponse.json({ errors: ["held evaluations cannot be changed"] }, { status: 409 });
  }

  const parsed = await parseObjectBody(request);
  if (!parsed.ok) {
    return NextResponse.json({ errors: ["invalid JSON body"] }, { status: 400 });
  }

  // StateManager.save_grade_usage_override, reached through the sidecar,
  // remains the sole domain validator (mode membership, exclude's
  // non-blank reason requirement) — this route only enforces the trusted
  // write context and JSON shape, matching web/app/api/grades/[evaluationId].
  const { status, body: respBody } = await callSidecar(
    `/grades/${evaluationId}/usage-override`,
    parsed.body
  );
  return NextResponse.json(respBody, { status });
}
