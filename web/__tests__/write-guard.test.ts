import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { isTrustedWriteContext } from "@/lib/write-guard";

function request(headers: Record<string, string>) {
  const values = new Map(
    Object.entries(headers).map(([key, value]) => [key.toLowerCase(), value])
  );
  return { headers: { get: (name: string) => values.get(name.toLowerCase()) ?? null } };
}

// Sole supported posture after this cohort: the trusted-LAN write boundary.
// No client (browser or otherwise) supplies a shared secret — trust is
// derived entirely from Host/Origin/Referer and single-hop forwarding
// checks against loopback + SCOUT_WEB_TRUSTED_HOSTS.
describe("trusted write context", () => {
  afterEach(() => {
    delete process.env.SCOUT_WEB_TRUSTED_HOSTS;
    delete process.env.SCOUT_WEB_TRUST_NEXT_FORWARDED_HEADERS;
  });

  it("accepts a direct loopback request with no headers beyond Host", () => {
    expect(
      isTrustedWriteContext(request({ host: "localhost:3000" }) as never)
    ).toBe(true);
  });

  it("accepts a loopback request with a same-origin browser Origin header", () => {
    expect(
      isTrustedWriteContext(
        request({
          host: "127.0.0.1:3000",
          origin: "http://127.0.0.1:3000",
        }) as never
      )
    ).toBe(true);
  });

  describe("configured web write hosts", () => {
    beforeEach(() => {
      process.env.SCOUT_WEB_TRUSTED_HOSTS = "scout.internal";
      process.env.SCOUT_WEB_TRUST_NEXT_FORWARDED_HEADERS = "1";
    });

    it("accepts a trusted browser host with Next.js synthesized forwarding headers", () => {
      expect(
        isTrustedWriteContext(
          request({
            host: "scout.internal:3001",
            origin: "http://scout.internal:3001",
            "x-forwarded-for": "172.18.0.1",
            "x-forwarded-host": "scout.internal:3001",
          }) as never
        )
      ).toBe(true);
    });

    it("rejects an unconfigured browser host", () => {
      expect(
        isTrustedWriteContext(
          request({
            host: "attacker.example:3001",
            origin: "http://attacker.example:3001",
            "x-forwarded-for": "172.18.0.1",
            "x-forwarded-host": "attacker.example:3001",
          }) as never
        )
      ).toBe(false);
    });

    it("rejects a forwarded host that disagrees with Host", () => {
      expect(
        isTrustedWriteContext(
          request({
            host: "scout.internal:3001",
            origin: "http://scout.internal:3001",
            "x-forwarded-for": "172.18.0.1",
            "x-forwarded-host": "attacker.example:3001",
          }) as never
        )
      ).toBe(false);
    });

    it("rejects a multi-hop forwarded-for chain", () => {
      expect(
        isTrustedWriteContext(
          request({
            host: "scout.internal:3001",
            origin: "http://scout.internal:3001",
            "x-forwarded-for": "192.0.2.10, 172.18.0.1",
            "x-forwarded-host": "scout.internal:3001",
          }) as never
        )
      ).toBe(false);
    });
  });

  it("rejects an untrusted Host with no SCOUT_WEB_TRUSTED_HOSTS configured", () => {
    expect(
      isTrustedWriteContext(
        request({
          host: "attacker.example:3000",
          origin: "http://attacker.example:3000",
        }) as never
      )
    ).toBe(false);
  });

  it("rejects a missing Host header", () => {
    expect(
      isTrustedWriteContext(request({ origin: "http://localhost:3000" }) as never)
    ).toBe(false);
  });

  it("rejects a trusted Host paired with an untrusted Origin", () => {
    expect(
      isTrustedWriteContext(
        request({
          host: "localhost:3000",
          origin: "http://attacker.example:3000",
        }) as never
      )
    ).toBe(false);
  });

  it("rejects a trusted Host paired with an untrusted Referer", () => {
    expect(
      isTrustedWriteContext(
        request({
          host: "localhost:3000",
          referer: "http://attacker.example:3000/steal",
        }) as never
      )
    ).toBe(false);
  });

  it("rejects forwarding headers on an otherwise-trusted loopback request when forwarding trust is not enabled", () => {
    expect(
      isTrustedWriteContext(
        request({
          host: "localhost:3000",
          "x-forwarded-for": "172.18.0.1",
          "x-forwarded-host": "localhost:3000",
        }) as never
      )
    ).toBe(false);
  });
});
