import { NextRequest } from "next/server";

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const TRUSTED_HOSTS_ENV = "SCOUT_WEB_TRUSTED_HOSTS";
const TRUST_NEXT_FORWARDING_ENV = "SCOUT_WEB_TRUST_NEXT_FORWARDED_HEADERS";

function isEnabledFlag(value: string | undefined): boolean {
  return value === "1" || value?.toLowerCase() === "true";
}

function parseHostname(header: string): string {
  const value = header.trim().toLowerCase();
  if (value.startsWith("[")) {
    const end = value.indexOf("]");
    return end >= 0 ? value.slice(1, end) : value.slice(1);
  }
  if (value.includes(":") && value.indexOf(":") !== value.lastIndexOf(":")) {
    return value;
  }
  return value.split(":")[0];
}

function trustedHosts(): Set<string> {
  const configured = (process.env[TRUSTED_HOSTS_ENV] ?? "")
    .split(",")
    .map(parseHostname)
    .filter(Boolean);
  return new Set([...LOOPBACK_HOSTS, ...configured]);
}

function hasTrustedForwardingHeaders(request: NextRequest, host: string): boolean {
  const h = request.headers;
  const forwardedFor = h.get("x-forwarded-for");
  const forwardedHost = h.get("x-forwarded-host");

  if (h.get("forwarded") !== null) return false;
  if (forwardedFor === null && forwardedHost === null) return true;
  if (!isEnabledFlag(process.env[TRUST_NEXT_FORWARDING_ENV])) return false;

  // Next.js synthesizes both headers before invoking route handlers. Only
  // accept its direct, single-hop shape and require the forwarded host to
  // agree with the already-validated Host header.
  if (forwardedFor === null || forwardedFor.trim() === "" || forwardedFor.includes(",")) {
    return false;
  }
  if (forwardedHost === null || forwardedHost.includes(",")) return false;
  return parseHostname(forwardedHost) === host;
}

// Returns true when the request originates from the trusted-LAN write
// boundary: a loopback Host, or a hostname explicitly listed in
// SCOUT_WEB_TRUSTED_HOSTS, with Host/Origin/Referer agreement and (when
// enabled) Next.js's single-hop synthesized forwarding headers. There is no
// credential a browser can supply on its own — trust is derived entirely
// from request context, not a shared secret.
export function isTrustedWriteContext(request: NextRequest): boolean {
  const h = request.headers;
  const allowedHosts = trustedHosts();

  const host = h.get("host");
  if (host === null) return false;
  const hostname = parseHostname(host);
  if (!allowedHosts.has(hostname)) return false;
  if (!hasTrustedForwardingHeaders(request, hostname)) return false;

  const origin = h.get("origin");
  if (origin !== null) {
    try {
      if (!allowedHosts.has(parseHostname(new URL(origin).hostname))) return false;
    } catch {
      return false;
    }
  }

  const referer = h.get("referer");
  if (referer !== null) {
    try {
      if (!allowedHosts.has(parseHostname(new URL(referer).hostname))) return false;
    } catch {
      return false;
    }
  }

  return true;
}
