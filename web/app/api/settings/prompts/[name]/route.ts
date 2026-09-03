import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import {
  updatePromptTemplate,
  setPromptTemplateActive,
} from "@/lib/settings-queries";
import { updatePromptTemplateSchema } from "@/lib/settings-schemas";
import { isTrustedWriteContext } from "@/lib/write-guard";

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { error: "write operations require a trusted network context" },
      { status: 403 }
    );
  }
  const { name } = await params;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const parsed = updatePromptTemplateSchema.safeParse(body);
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
  const result = updatePromptTemplate(name, parsed.data);
  if (!result.ok) {
    const status = result.error === "not_found" ? 404 : 400;
    return NextResponse.json({ error: result.message }, { status });
  }
  return NextResponse.json(result.data);
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { error: "write operations require a trusted network context" },
      { status: 403 }
    );
  }
  const { name } = await params;
  const confirm =
    new URL(request.url).searchParams.get("confirm") === "true";
  const result = setPromptTemplateActive(name, false, confirm);
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
  { params }: { params: Promise<{ name: string }> }
) {
  if (!isTrustedWriteContext(request)) {
    return NextResponse.json(
      { error: "write operations require a trusted network context" },
      { status: 403 }
    );
  }
  const { name } = await params;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const parsed = z
    .object({ active: z.boolean(), confirm: z.boolean().optional() })
    .safeParse(body);
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
  const { active, confirm = false } = parsed.data;
  const result = setPromptTemplateActive(name, active, confirm);
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
