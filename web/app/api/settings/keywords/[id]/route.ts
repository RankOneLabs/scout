import { NextRequest, NextResponse } from "next/server";
import {
  updateKeyword,
  setKeywordActive,
  deleteKeyword,
} from "@/lib/settings-queries";
import { updateKeywordSchema, patchActiveSchema } from "@/lib/settings-schemas";
import { parseIdParam } from "@/lib/route-utils";
import { isTrustedWriteContext } from "@/lib/write-guard";

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { error: "write operations require a trusted network context" },
      { status: 403 }
    );
  }
  const { id: rawId } = await params;
  const id = parseIdParam(rawId);
  if (id === null) {
    return NextResponse.json({ error: "invalid keyword id" }, { status: 400 });
  }
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const parsed = updateKeywordSchema.safeParse(body);
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
  const result = updateKeyword(id, parsed.data);
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

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { error: "write operations require a trusted network context" },
      { status: 403 }
    );
  }
  const { id: rawId } = await params;
  const id = parseIdParam(rawId);
  if (id === null) {
    return NextResponse.json({ error: "invalid keyword id" }, { status: 400 });
  }
  const result = deleteKeyword(id);
  if (!result.ok) {
    const status = result.error === "not_found" ? 404 : 400;
    return NextResponse.json({ error: result.message }, { status });
  }
  return NextResponse.json(result.data);
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { error: "write operations require a trusted network context" },
      { status: 403 }
    );
  }
  const { id: rawId } = await params;
  const id = parseIdParam(rawId);
  if (id === null) {
    return NextResponse.json({ error: "invalid keyword id" }, { status: 400 });
  }
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
  const result = setKeywordActive(id, parsed.data.active);
  if (!result.ok) {
    const status = result.error === "not_found" ? 404 : 400;
    return NextResponse.json({ error: result.message }, { status });
  }
  return NextResponse.json(result.data);
}
