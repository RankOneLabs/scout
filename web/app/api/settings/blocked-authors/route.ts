import { NextRequest, NextResponse } from "next/server";
import { blockAuthor, listBlockedAuthors } from "@/lib/settings-queries";
import { blockAuthorSchema, listFiltersSchema } from "@/lib/settings-schemas";
import { parseSearchParams } from "@/lib/filter-schemas";
import { isTrustedWriteContext } from "@/lib/write-guard";

export async function GET(request: NextRequest) {
  const parsed = parseSearchParams(
    request.nextUrl.searchParams,
    listFiltersSchema
  );
  if (!parsed.ok) {
    return NextResponse.json({ errors: parsed.errors }, { status: 400 });
  }
  return NextResponse.json({
    blocked_authors: listBlockedAuthors(parsed.data.include_inactive),
  });
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
  const parsed = blockAuthorSchema.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json(
      {
        errors: parsed.error.issues.map(
          (issue) => `${issue.path.join(".")}: ${issue.message}`
        ),
      },
      { status: 400 }
    );
  }
  const result = blockAuthor(parsed.data);
  if (!result.ok) {
    return NextResponse.json({ error: result.message }, { status: 400 });
  }
  return NextResponse.json(result.data, { status: 201 });
}
