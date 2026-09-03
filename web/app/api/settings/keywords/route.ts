import { NextRequest, NextResponse } from "next/server";
import {
  listKeywords,
  createKeyword,
  createKeywords,
} from "@/lib/settings-queries";
import {
  createKeywordSchema,
  createKeywordsSchema,
  keywordListFiltersSchema,
} from "@/lib/settings-schemas";
import { parseSearchParams } from "@/lib/filter-schemas";
import { isTrustedWriteContext } from "@/lib/write-guard";

export async function GET(request: NextRequest) {
  const parsed = parseSearchParams(
    request.nextUrl.searchParams,
    keywordListFiltersSchema
  );
  if (!parsed.ok) {
    return NextResponse.json({ errors: parsed.errors }, { status: 400 });
  }
  const keywords = listKeywords({
    projectKey: parsed.data.project_key,
    includeInactive: parsed.data.include_inactive,
  });
  return NextResponse.json({ keywords });
}

export async function POST(request: NextRequest) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { error: "write operations require a trusted network context" },
      { status: 403 }
    );
  }
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const isBulkCreate =
    typeof body === "object" &&
    body !== null &&
    "keywords" in body &&
    Array.isArray(body.keywords);

  if (isBulkCreate) {
    const parsed = createKeywordsSchema.safeParse(body);
    if (!parsed.success) {
      return NextResponse.json(
        {
          errors: parsed.error.issues.map(
            (i) => `${i.path.join(".")}: ${i.message}`
          ),
        },
        { status: 400 }
      );
    }
    const result = createKeywords(parsed.data);
    if (!result.ok) {
      const status =
        result.error === "not_found"
          ? 404
          : result.error === "conflict"
            ? 409
            : 400;
      return NextResponse.json({ error: result.message }, { status });
    }
    return NextResponse.json(result.data, { status: 201 });
  }

  const parsed = createKeywordSchema.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json(
      {
        errors: parsed.error.issues.map(
          (i) => `${i.path.join(".")}: ${i.message}`
        ),
      },
      { status: 400 }
    );
  }
  const result = createKeyword(parsed.data);
  if (!result.ok) {
    const status =
      result.error === "not_found"
        ? 404
        : result.error === "conflict"
          ? 409
          : 400;
    return NextResponse.json({ error: result.message }, { status });
  }
  return NextResponse.json(result.data, { status: 201 });
}
