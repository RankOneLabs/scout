import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { isTrustedWriteContext } from "@/lib/write-guard";
import { parseSearchParams, utcInstant } from "@/lib/feedback-filters";
import { getFeedbackSummary } from "@/lib/feedback-summary-queries";

const DEFAULT_WINDOW_DAYS = 90;

const summaryQuerySchema = z.object({
  from: utcInstant.optional(),
  to: utcInstant.optional(),
});

// Corpus metrics, coverage, relevance, acceptance, and eligibility all
// carry prompt-relevant grade notes and post content — reuse the same
// trusted-host boundary the feedback snapshot routes use.
export async function GET(request: NextRequest) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { errors: ["this endpoint requires a trusted network context"] },
      { status: 403 }
    );
  }

  const parsed = parseSearchParams(request.nextUrl.searchParams, summaryQuerySchema);
  if (!parsed.ok) {
    return NextResponse.json({ errors: parsed.errors }, { status: 400 });
  }

  const asOf = new Date().toISOString();
  const to = parsed.data.to ?? asOf;
  const from = parsed.data.from ?? new Date(Date.parse(asOf) - DEFAULT_WINDOW_DAYS * 24 * 60 * 60 * 1000).toISOString();

  if (Date.parse(from) > Date.parse(to)) {
    return NextResponse.json({ errors: ["from: must be less than or equal to to"] }, { status: 400 });
  }

  return NextResponse.json(getFeedbackSummary(asOf, from, to));
}
