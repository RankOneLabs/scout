# Deployment security

Scout has two distinct HTTP write boundaries: the Python grading sidecar and
the Next.js operator UI. Neither should be exposed directly to an untrusted
network.

## Grading API sidecar

The sidecar entry point is `scout.cli.grading_api`. When
`SCOUT_SIDECAR_HOST` is unset, it binds to `127.0.0.1`; an empty
`SCOUT_SIDECAR_TOKEN` is permitted only for this local-development mode.

A non-loopback bind, including `0.0.0.0`, requires a non-empty token. The
process refuses to start without one. Generate a token with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Container deployments should also name the host clients use in the `Host`
header:

```dotenv
SCOUT_SIDECAR_HOST=0.0.0.0
SCOUT_SIDECAR_TRUSTED_HOST=scout-sidecar
SCOUT_SIDECAR_TOKEN=...
```

When the bind address is a concrete hostname or IP rather than a wildcard,
the trusted host defaults to that value.

The frontend reads `SCOUT_SIDECAR_TOKEN` on the server and forwards it as
`X-Scout-Sidecar-Token`. Never expose it through a `NEXT_PUBLIC_*` variable.
A browser-supplied sidecar-token header is not forwarded.

`POST /grades/{evaluation_id}` accepts only the shared grade payload. The
sidecar owns identity, source, schema version, timestamp, and eligibility;
requests attempting to set those fields are rejected.

## Web API write boundary

Mutating Next.js routes validate request context as a CSRF and DNS-rebinding
boundary. This validation is not user authentication: `Host`, `Origin`, and
`Referer` are client-controlled. Restrict access separately with a loopback
bind, firewall, or identity-enforcing reverse proxy.

Requests are accepted for loopback hosts or hosts explicitly listed in
`SCOUT_WEB_TRUSTED_HOSTS`:

```dotenv
SCOUT_WEB_TRUSTED_HOSTS=scout.internal
SCOUT_WEB_TRUST_NEXT_FORWARDED_HEADERS=1
```

Only trust a hostname on a network where its clients are trusted. Forwarding
headers are rejected by default. The opt-in forwarded-header setting accepts
only Next.js's single-hop synthesized shape when its forwarded host agrees
with the validated `Host`; leave it unset for a direct bind.

For write requests:

- `Host` must be present and be loopback or explicitly trusted.
- Present `Origin` and `Referer` values must also resolve to an allowed host.
- Forwarding headers are rejected unless the explicit Next.js mode is enabled.
- `/healthz` remains unauthenticated and bypasses these checks.

## Credential rotation after an unsafe image build

Docker layer history can retain files removed by a later layer. If an image
was built before `.dockerignore` excluded local secrets, rotate every
credential that could have appeared in the build context:

- Discord bot token
- Anthropic and OpenRouter API keys
- Neynar API key
- Bluesky app password
- Farcaster developer mnemonic and its associated custody wallet

Update the local `.env` only after revoking the old credentials. Never commit
the replacement values.
