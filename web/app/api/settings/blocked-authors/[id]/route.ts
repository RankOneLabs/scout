import { NextRequest, NextResponse } from "next/server";
import { unblockAuthor } from "@/lib/settings-queries";
import { parseIdParam } from "@/lib/route-utils";
import { isTrustedWriteContext } from "@/lib/write-guard";

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
    return NextResponse.json(
      { error: "invalid blocked author id" },
      { status: 400 }
    );
  }
  const result = unblockAuthor(id);
  if (!result.ok) {
    const status = result.error === "not_found" ? 404 : 400;
    return NextResponse.json({ error: result.message }, { status });
  }
  return NextResponse.json(result.data);
}
