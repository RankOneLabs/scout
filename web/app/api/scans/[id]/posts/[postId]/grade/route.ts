import { NextRequest, NextResponse } from "next/server";
import {
  getEvaluationForScanPost,
  getGradeByScanPost,
} from "@/lib/queries";
import { callSidecar, parseObjectBody } from "@/lib/sidecar-bridge";
import { validateGradeEnvelope } from "@/lib/grade-validation";
import { parseIdParam } from "@/lib/route-utils";
import { isTrustedWriteContext } from "@/lib/write-guard";

type RouteParams = { params: Promise<{ id: string; postId: string }> };

export async function GET(
  _request: NextRequest,
  { params }: RouteParams
) {
  const { id: rawScanId, postId: rawPostId } = await params;
  const numScanId = parseIdParam(rawScanId);
  const numPostId = parseIdParam(rawPostId);
  if (numScanId === null || numPostId === null) {
    return NextResponse.json(
      { errors: ["invalid scan or post id"] },
      { status: 400 }
    );
  }
  const grade = getGradeByScanPost(numPostId, numScanId);
  return NextResponse.json(grade);
}

export async function POST(
  request: NextRequest,
  { params }: RouteParams
) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { errors: ["write operations require a trusted network context"] },
      { status: 403 }
    );
  }
  const { id: rawScanId, postId: rawPostId } = await params;
  const numScanId = parseIdParam(rawScanId);
  const numPostId = parseIdParam(rawPostId);
  if (numScanId === null || numPostId === null) {
    return NextResponse.json(
      { errors: ["invalid scan or post id"] },
      { status: 400 }
    );
  }

  // Resolve the exact evaluation for this scan/post pair through a
  // read-only query — 404 unless that exact pair resolves to an
  // evaluation, so a post can't be graded against the wrong scan.
  const evaluation = getEvaluationForScanPost(numPostId, numScanId);
  if (evaluation === null) {
    return NextResponse.json(
      { errors: [`Post ${numPostId} not found in scan ${numScanId}`] },
      { status: 404 }
    );
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
    `/grades/${evaluation.id}`,
    parsed.body
  );
  return NextResponse.json(respBody, { status });
}
