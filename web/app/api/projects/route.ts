import { NextResponse } from "next/server";
import { getProjectKeys } from "@/lib/queries";

export function GET() {
  const keys = getProjectKeys();
  return NextResponse.json(keys);
}
