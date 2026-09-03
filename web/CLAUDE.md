# CLAUDE.md — scout web

Browse and review scout scan results. Reads the SQLite database produced by the Python scanner and presents scans, posts, evaluations, draft comments, and critiques in a web UI.

## Quick Reference

```bash
cd web
npm run dev       # Next.js dev server
npm run build     # TypeScript check + Next.js build
npm test          # Vitest
npm run lint      # ESLint
```

## Tech Stack

- **Next.js 16.1.6** (App Router), **React 19**, **TypeScript**
- **Tailwind CSS 4** — all styling via utility classes
- **better-sqlite3** — read `../scout.db` in API routes (server-side only)
- **Vitest** — unit tests for lib modules

## Project Structure

```
web/
├── app/
│   ├── layout.tsx                # Root layout — html, body, fonts, global providers
│   ├── page.tsx                  # Dashboard — scan stats overview
│   ├── scans/
│   │   ├── page.tsx              # Scan list
│   │   └── [id]/page.tsx         # Single scan detail
│   ├── posts/
│   │   └── page.tsx              # Post browser with filters
│   ├── drafts/
│   │   └── page.tsx              # Draft comments with critique verdicts
│   └── api/
│       ├── scans/route.ts        # GET /api/scans
│       ├── posts/route.ts        # GET /api/posts
│       ├── evaluations/route.ts  # GET /api/evaluations
│       ├── drafts/route.ts       # GET /api/drafts
│       └── stats/route.ts        # GET /api/stats
├── lib/
│   ├── db.ts                     # better-sqlite3 singleton, opens ../scout.db
│   ├── queries.ts                # Named query functions — getScans(), getPosts(), etc.
│   └── transforms.ts             # Data shaping: group, sort, filter helpers
├── components/
│   ├── atoms/                    # Smallest reusable UI primitives (Badge, StatusDot, etc.)
│   ├── molecules/                # Composed atom groups (ListItem, FilterBar, etc.)
│   └── organisms/                # Full feature sections (ScanTable, PostList, DraftCard, etc.)
├── hooks/
│   └── use-*.ts                  # Data fetching and UI state hooks
├── types/
│   └── schema.ts                 # TypeScript mirrors of Python domain types
└── __tests__/
    └── *.test.ts                 # Vitest tests for lib modules
```

## Data Flow

1. **API routes** (server-side) open `../scout.db` via `better-sqlite3`, run queries, return JSON
2. **Client components** fetch from `/api/*` via custom hooks
3. **Transforms** in `lib/transforms.ts` shape the response data for display
4. **Components** render the transformed data

All database access is server-side only. `better-sqlite3` must never be imported in client code.

## Domain Types (`types/schema.ts`)

TypeScript mirrors of the Python-side domain types and SQLite schema:

```ts
interface Scan {
  id: number
  started_at: string
  completed_at: string | null
  messages_scanned: number
  relevant_found: number
}

interface Post {
  id: number
  platform: string          // "discord" | "farcaster"
  platform_msg_id: string
  channel_name: string
  channel_id: string
  author_name: string
  author_id: string
  content: string
  url: string
  created_at: string
  scan_id: number
}

interface Evaluation {
  id: number
  post_id: number
  relevant: boolean
  score: number
  reason: string
  relevant_to: string[]     // JSON-parsed from TEXT column
  scan_id: number
}

interface DraftComment {
  id: number
  post_id: number
  evaluation_id: number
  project_key: string
  comment_text: string
  created_at: string
  scan_id: number
}

interface Critique {
  id: number
  draft_id: number
  verdict: string           // "approve" | "revise" | "reject"
  feedback: string
  created_at: string
  scan_id: number
}

interface ScanStats {
  total_scans: number
  total_posts: number
  total_relevant: number
  total_drafts: number
}
```

## Component Architecture (Atomic Design)

Components follow atomic design with three tiers:

- **Atoms** (`components/atoms/`) — Smallest reusable UI primitives. No business logic, no state. Accept simple props and render a single visual element. Examples: `Badge`, `StatusDot`, `IconButton`.
- **Molecules** (`components/molecules/`) — Compositions of 2+ atoms into a reusable UI group. May have minimal local state. Examples: `FilterBar`, `ListItem`, `StatCard`.
- **Organisms** (`components/organisms/`) — Full feature sections that compose atoms + molecules with data and logic. These are what page components render. Examples: `ScanTable`, `PostList`, `DraftCard`, `CritiquePanel`.

When adding a new component, classify it into the appropriate tier. If you're unsure, ask: "Does it render a single element?" (atom), "Does it compose atoms?" (molecule), or "Does it implement a feature?" (organism).

## Code Style

Bias toward `map`, `filter`, `reduce` pipelines over imperative `for` loops. This matches the parent Python CLAUDE.md philosophy — data transformations should be built from composable operations, not inline mutation.

- **Default to pipelines.** Accumulate into Maps, Sets, and grouped structures via `reduce`. Extract named helpers when a transform step is reusable or the pipeline gets long.
- **Each helper should be independently testable.** Pure functions in `lib/transforms.ts` that take typed input and return typed output.
- **Loops are acceptable** for genuinely stateful operations — DOM side effects, imperative API calls. These are not data transforms.

### Component conventions

- All components are functional `.tsx` with typed props interfaces
- Styling is 100% Tailwind utility classes — no CSS modules, no styled-components
- Props use `on*` prefix for callbacks (`onClose`, `onFilter`, `onSelect`)
- Destructure props in the function signature

## State Management

No Redux, Zustand, or other state libraries. All state lives in custom hooks (`hooks/use-*.ts`) that encapsulate fetching, filtering, and UI state. Hooks return named values and setters.

## Styling

100% Tailwind utility classes. No CSS modules, no styled-components, no inline style objects. Use Tailwind's built-in responsive and dark mode utilities.

## Testing

Tests live in `__tests__/*.test.ts` — pure logic tests for `lib/` modules only. No component rendering tests.

- Test data transforms in `lib/transforms.ts`
- Test query result shaping in `lib/queries.ts` (with an in-memory SQLite fixture if needed)
- All tests use Vitest `describe`/`it`/`expect` patterns

## Grading sidecar bridge

The grading write surface lives in Python. Every write route the
dashboard exposes — `POST /api/grades/[evaluationId]`, its `promote` and
`usage-override` siblings, and `POST /api/scans/[id]/posts/[postId]/grade`
— proxies to the FastAPI sidecar in `src/scout/cli/grading_api.py`,
via `lib/sidecar-bridge.ts` (`callSidecar`, `parseObjectBody`). Going
through Python is required because a grade save revalidates the shared
grading contract with full stored context, promotion runs the jig drafter
and critic, and both have feedback-loop side effects with no JS port.
Reads stay in Next.js against `../scout.db` through `lib/db.ts`.

### Dev loop

Run the sidecar in one terminal and Next.js in another:

```bash
# Terminal 1 — at the repo root
uv run scout-grading-api          # binds 127.0.0.1:8799 by default

# Terminal 2 — inside web/
npm run dev
```

The sidecar reads `SCOUT_SIDECAR_PORT` (default `8799`) and
`SCOUT_SIDECAR_TOKEN` (optional shared secret). When the token is set,
every write request must echo it via `X-Scout-Sidecar-Token`. The Next.js
bridge reads both env vars at server start; set them on the Next.js
process too so the proxy can reach the sidecar. `__tests__/sidecar-e2e.test.ts`
launches the real sidecar with `uv run` and exercises this path end to end.
