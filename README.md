# Scout

Scout monitors Discord, Farcaster, and Bluesky for posts relevant to your
projects, evaluates them against project dossiers, and drafts engagement
comments. Outbound content creation and publishing live in a separate
application.

Every task that can affect the outside world is declared under the
[Progressive Autonomy Architecture](https://www.paa.dev) (PAA). Scout's
checked-in tasks begin with human approval and can advance only through an
evidence-backed promotion reviewed by an operator. No Scout task is deployed
at `autonomous` today. The PAA site describes where Scout fits on its
[implementations page](https://www.paa.dev/build/implementations#scout).

## Review PAA in Scout

Want the architecture and evidence without installing the application? Follow
[the three-minute PAA evidence tour](docs/paa-reviewer-walkthrough.md).

It covers Scout's task declarations, evaluator evidence, event-sourced autonomy
state, promotion and demotion mechanics, and the limits of the current deployment.

## Quick start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- A Discord bot token and server access
- An Anthropic API key, or another configured model provider

### Install

```bash
uv sync
cp .env.example .env
```

Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications),
enable **Message Content Intent**, and invite it with `View Channels` and
`Read Message History` permissions. Then set at least:

```dotenv
DISCORD_BOT_TOKEN=...
DISCORD_SERVER_ID=...
DISCORD_CHANNEL_IDS=...
ANTHROPIC_API_KEY=...
```

Provider routing, scan controls, and request-volume considerations are
documented in [Configuration](docs/configuration.md); `.env.example` is the
canonical environment-variable template.

### Run

```bash
# Run one scan
uv run scout

# Include debug logging
uv run scout --debug

# Scan every SCAN_INTERVAL_HOURS
uv run scout --continuous

# Show statistics
uv run scout --stats
```

Run `uv run scout --help` to see the PAA, evaluation, grading, and replay
commands.

## How it works

```text
Fetch messages → keyword prefilter → evaluate and draft → critique and revise → digest
```

1. Platform adapters fetch new messages and available parent context.
2. Keyword routing removes clearly irrelevant messages before model calls.
3. A typed Jig agent evaluates relevance, drafts a reply, critiques it, and
   optionally revises it.
4. Scout persists the results and writes a Markdown digest to `digests/`.

Projects, keywords, and prompt overrides are managed in the web settings UI.
See [Keyword routing and prompt overrides](docs/keyword-routing.md).

Before any live scan, Scout resolves every active project's dossier from a
clean checkout pinned to a full commit SHA. Run the non-writing deployment
gate explicitly with:

```bash
uv run scout preflight --dossier-root /path/to/dossier-source --db-path scout.db
```

See [Dossier contract](docs/dossier-contract.md) for the complete resolution,
schema, conformance, and readiness rules.

## Contract-governed autonomy

Scout declares two PAA tasks:

- [`inbound_reply_surfacing`](contracts/paa/inbound_reply_surfacing.v1.yaml) is
  deployed in `shadow` mode: it is evaluated but does not gate replies.
- [`canonical_promotion`](contracts/paa/canonical_promotion.v1.yaml) is
  `disabled`: it is declared but has no runtime enforcement point.

Operators can inspect and change autonomy positions with `scout paa`; all
changes are backed by an append-only event log and content-addressed evidence.
See [Architecture](docs/architecture.md) for the design and
[PAA operations](docs/runbooks/paa-operations.md) for commands and procedures.

Human grades also feed future prompts through immutable feedback snapshots.
Offline replay can compare candidate models and prompts without changing live
state. See [Grading and feedback](docs/grading-and-feedback.md) and
[Evaluations](docs/evaluations.md).

## Deployment status

Scout integrates PAA contracts and the event-sourced autonomy control plane, but
neither checked-in PAA task is wired to an active runtime enforcement point today.

| Task | Initial position | Deployment | Current effect |
| --- | --- | --- | --- |
| `inbound_reply_surfacing` | `hitl` | `shadow` | Evaluated and recorded; does not gate runtime behavior |
| `canonical_promotion` | `hitl` | `disabled` | Declared for future integration; no runtime effect |

Position changes exercised through the reference path demonstrate the control-plane
mechanics. They are not evidence that Scout has earned or operated at HOTL or
autonomous status in production. Production-derived grading and feedback evidence
stays in the deployment that produced it and is exported only in redacted,
publication-safe form; the evidence checked into this repository is reference
evidence rendered from fixture data.

Position is deployment-local and reconstructed from its event stream, so this
README publishes no "current position" value.

## Project structure

```text
├── src/scout/            Python package
│   ├── cli/              CLI and grading API entry points
│   ├── dossiers/         Dossier resolution and contract enforcement
│   ├── platforms/        Discord, Farcaster, and Bluesky adapters
│   ├── scanning/         Agent pipeline and scan orchestration
│   ├── storage/          SQLite schema, migrations, and stores
│   ├── grading/          Human grading, feedback, and corpus operations
│   ├── replay/           Offline experiments, pricing, and reporting
│   ├── paa/              Autonomy declarations, evidence, and audits
│   ├── evals/phase1/     Phase 1 corpus, grader, and sweep runner
│   └── prompts/          Packaged prompt fragments
├── web/                  Next.js operator UI
├── contracts/            Versioned external contracts and schemas
├── evidence/             Publication-safe reference evidence
├── docs/                 Architecture, guides, and runbooks
├── scripts/              Maintenance and audit commands
└── tests/                Python tests and fixtures
```

## Documentation

- [Documentation index](docs/README.md)
- [PAA reviewer walkthrough](docs/paa-reviewer-walkthrough.md)
- [Configuration](docs/configuration.md)
- [Deployment security](docs/deployment-security.md)
- [Evaluations](docs/evaluations.md)
- [Grading and feedback](docs/grading-and-feedback.md)
- [Dossier contract](docs/dossier-contract.md)
- [Platform adapters](docs/platform-adapters.md)
- [PAA operator runbook](docs/runbooks/paa-operations.md)

## License

Apache License 2.0 — see [LICENSE](LICENSE).
