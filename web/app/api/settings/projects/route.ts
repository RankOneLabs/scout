import { NextRequest, NextResponse } from "next/server";
import { listProjects, createProject } from "@/lib/settings-queries";
import {
  createProjectSchema,
  listFiltersSchema,
} from "@/lib/settings-schemas";
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
  const projects = listProjects(parsed.data.include_inactive);
  return NextResponse.json({ projects });
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
  const parsed = createProjectSchema.safeParse(body);
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
  const result = createProject(parsed.data);
  if (!result.ok) {
    const status = result.error === "conflict" ? 409 : 400;
    return NextResponse.json({ error: result.message }, { status });
  }
  return NextResponse.json(result.data, { status: 201 });
}
