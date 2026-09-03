import { NextRequest, NextResponse } from "next/server";
import {
  updateProject,
  setProjectActive,
  deleteProjectIfUnreferenced,
} from "@/lib/settings-queries";
import { updateProjectSchema, patchActiveSchema } from "@/lib/settings-schemas";
import { isTrustedWriteContext } from "@/lib/write-guard";

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ key: string }> }
) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { error: "write operations require a trusted network context" },
      { status: 403 }
    );
  }
  const { key } = await params;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const parsed = updateProjectSchema.safeParse(body);
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
  const result = updateProject(key, parsed.data);
  if (!result.ok) {
    const status = result.error === "not_found" ? 404 : 400;
    return NextResponse.json({ error: result.message }, { status });
  }
  return NextResponse.json(result.data);
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ key: string }> }
) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { error: "write operations require a trusted network context" },
      { status: 403 }
    );
  }
  const { key } = await params;
  const result = deleteProjectIfUnreferenced(key);
  if (!result.ok) {
    const status =
      result.error === "not_found"
        ? 404
        : result.error === "conflict"
          ? 409
          : 400;
    return NextResponse.json({ error: result.message }, { status });
  }
  return NextResponse.json(result.data);
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ key: string }> }
) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { error: "write operations require a trusted network context" },
      { status: 403 }
    );
  }
  const { key } = await params;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const parsed = patchActiveSchema.safeParse(body);
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
  const result = setProjectActive(key, parsed.data.active);
  if (!result.ok) {
    const status = result.error === "not_found" ? 404 : 400;
    return NextResponse.json({ error: result.message }, { status });
  }
  return NextResponse.json(result.data);
}
