import { NextRequest, NextResponse } from "next/server";

import { callSidecar, parseObjectBody } from "@/lib/sidecar-bridge";
import { validateGradeEnvelope } from "@/lib/grade-validation";
import { getEvaluationById } from "@/lib/queries";
import { parseIdParam } from "@/lib/route-utils";
import { isTrustedWriteContext } from "@/lib/write-guard";

type RouteParams = { params: Promise<{ evaluationId: string }> };

export async function POST(request: NextRequest, { params }: RouteParams) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { errors: ["write operations require a trusted network context"] },
      { status: 403 }
    );
  }
  const evaluationId = parseIdParam((await params).evaluationId);
  if (evaluationId === null) {
    return NextResponse.json({ errors: ["invalid evaluation id"] }, { status: 400 });
  }
  const evaluation = getEvaluationById(evaluationId);
  if (evaluation === null) {
    return NextResponse.json(
      { errors: [`Evaluation ${evaluationId} not found`] },
      { status: 404 }
    );
  }
  if (evaluation.relevant) {
    return NextResponse.json(
      { errors: ["only model-negative evaluations can enter the promotion flow"] },
      { status: 409 }
    );
  }
  const parsed = await parseObjectBody(request);
  if (!parsed.ok) {
    return NextResponse.json({ errors: ["invalid JSON body"] }, { status: 400 });
  }
  const errors = validateGradeEnvelope(parsed.body, evaluation.posture);
  if (errors.length > 0) {
    return NextResponse.json({ errors }, { status: 400 });
  }
  if (parsed.body.relevance_judgment !== "false_negative") {
    return NextResponse.json(
      { errors: ["promotion requires relevance_judgment=false_negative"] },
      { status: 400 }
    );
  }

  const { status, body } = await callSidecar(
    `/grades/${evaluationId}/promote`,
    parsed.body,
    { timeoutMs: 180_000 }
  );
  return NextResponse.json(body, { status });
}
