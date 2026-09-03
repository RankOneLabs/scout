import { NextRequest, NextResponse } from "next/server";
import { getEvaluationById, getGradeByEvaluationId } from "@/lib/queries";
import { callSidecar, parseObjectBody } from "@/lib/sidecar-bridge";
import { validateGradeEnvelope } from "@/lib/grade-validation";
import { parseIdParam } from "@/lib/route-utils";
import { isTrustedWriteContext } from "@/lib/write-guard";

type RouteParams = { params: Promise<{ evaluationId: string }> };

export async function GET(_request: NextRequest, { params }: RouteParams) {
  const evaluationId = parseIdParam((await params).evaluationId);
  if (evaluationId === null) return NextResponse.json({ errors: ["invalid evaluation id"] }, { status: 400 });
  return NextResponse.json(getGradeByEvaluationId(evaluationId));
}

export async function POST(request: NextRequest, { params }: RouteParams) {
  if (!isTrustedWriteContext(request)) return NextResponse.json({ errors: ["write operations require a trusted network context"] }, { status: 403 });
  const evaluationId = parseIdParam((await params).evaluationId);
  if (evaluationId === null) return NextResponse.json({ errors: ["invalid evaluation id"] }, { status: 400 });

  // Resolve the exact evaluation row through a read-only query so a
  // nonexistent evaluation 404s before the sidecar is ever called.
  const evaluation = getEvaluationById(evaluationId);
  if (evaluation === null) {
    return NextResponse.json({ errors: [`Evaluation ${evaluationId} not found`] }, { status: 404 });
  }

  const parsed = await parseObjectBody(request);
  if (!parsed.ok) {
    return NextResponse.json({ errors: ["invalid JSON body"] }, { status: 400 });
  }

  // Fail fast on the shared contract before ever reaching the sidecar.
  // StateManager.save_grade revalidates the same envelope server-side and
  // remains the only authoritative persistence boundary — this is purely
  // an early rejection to avoid a round trip for an already-invalid grade.
  const envelopeErrors = validateGradeEnvelope(parsed.body, evaluation.posture);
  if (envelopeErrors.length > 0) {
    return NextResponse.json({ errors: envelopeErrors }, { status: 400 });
  }

  const { status, body: respBody } = await callSidecar(
    `/grades/${evaluationId}`,
    parsed.body
  );
  return NextResponse.json(respBody, { status });
}
