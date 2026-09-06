import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { POST } from "@/app/api/grading/queues/[digest]/evaluations/[evaluationId]/actions/route";
import { callSidecar } from "@/lib/sidecar-bridge";
import { isTrustedWriteContext } from "@/lib/write-guard";

vi.mock("@/lib/sidecar-bridge", async (original) => ({ ...await original<typeof import("@/lib/sidecar-bridge")>(), callSidecar: vi.fn() }));
vi.mock("@/lib/write-guard", () => ({ isTrustedWriteContext: vi.fn(() => true) }));
beforeEach(() => { vi.clearAllMocks(); vi.mocked(isTrustedWriteContext).mockReturnValue(true); });
describe("review action proxy", () => {
  it("forwards the exact evaluation, action ID and timing without promoting", async () => {
    vi.mocked(callSidecar).mockResolvedValue({ status: 200, body: { action_id: "synthetic" } });
    const payload = { action_id: "synthetic", timing: { elapsed_ms: 1000 }, action: { kind: "skip", reason: "Unsure" } };
    const response = await POST(new NextRequest("http://localhost/api", { method: "POST", body: JSON.stringify(payload) }), { params: Promise.resolve({ digest: "a".repeat(64), evaluationId: "123" }) });
    expect(response.status).toBe(200);
    expect(callSidecar).toHaveBeenCalledWith(`/review-queues/${"a".repeat(64)}/evaluations/123/actions`, payload);
  });
  it("rejects untrusted writes before the bridge", async () => {
    vi.mocked(isTrustedWriteContext).mockReturnValue(false);
    const response = await POST(new NextRequest("http://localhost/api", { method: "POST" }), { params: Promise.resolve({ digest: "a".repeat(64), evaluationId: "123" }) });
    expect(response.status).toBe(403);
    expect(callSidecar).not.toHaveBeenCalled();
  });
  it("preserves revision conflicts from the authoritative writer", async () => {
    vi.mocked(callSidecar).mockResolvedValue({ status: 409, body: { detail: "Grade changed" } });
    const response = await POST(new NextRequest("http://localhost/api", { method: "POST", body: "{}" }), { params: Promise.resolve({ digest: "a".repeat(64), evaluationId: "123" }) });
    expect(response.status).toBe(409);
  });
});
