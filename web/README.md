This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deployment security

### Intended access boundary

The Scout web UI and its API routes are designed for **trusted-LAN access
only**. There is no browser-facing write credential — a browser cannot
safely hold a shared secret, so mutating routes (project settings, keyword
configuration, candidate approval, feedback submission) are instead
authorized by request context: a trusted loopback `Host`
(`localhost`/`127.0.0.1`/`::1`) or a hostname explicitly listed in
`SCOUT_WEB_TRUSTED_HOSTS`, with `Host`/`Origin`/`Referer` agreement and (when
enabled via `SCOUT_WEB_TRUST_NEXT_FORWARDED_HEADERS`) Next.js's own
single-hop synthesized forwarding headers. See the root `README.md`'s "Web
API write boundary" section for the full check list. Only add a hostname to
`SCOUT_WEB_TRUSTED_HOSTS` on a network where every client on it is trusted —
this does not add browser login or user authentication.

Every grade write route (`POST /api/grades/[evaluationId]`, its `promote`
and `usage-override` siblings, and `POST /api/scans/[id]/posts/[postId]/grade`)
runs that guard check itself and then relays through `callSidecar`
(`web/lib/sidecar-bridge.ts`), the single place the sidecar relay is
implemented — routes pass a fixed sidecar path, never an arbitrary one, so
the helper can't become a generic SSRF primitive.

### Secrets and the browser boundary

`SCOUT_SIDECAR_TOKEN` is used server-side to authenticate requests from the
Next.js API layer to the Python grading sidecar. It is read only by
server-side bridge code (`web/lib/sidecar-bridge.ts`) and attached only to
outgoing sidecar requests — no client-supplied header is ever forwarded to
the sidecar. It must never be placed in a `NEXT_PUBLIC_*` variable — doing so
would embed the token in client JavaScript bundles and expose it to any user
who can load the page.

When running in a container, set `SCOUT_SIDECAR_TOKEN` in the server
environment only (e.g., Docker `--env` or `env_file`), not in `.env.local`
with a `NEXT_PUBLIC_` prefix.

### Docker build context

The `web/.dockerignore` file excludes `.env`, `.env.*`, `node_modules/`, `.next/`, and build-info files from the Docker build context. Do not remove or bypass these exclusions — they prevent credentials and host-local state from entering image layers.
