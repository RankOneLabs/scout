# Configuration

Scout reads settings from environment variables and loads a local `.env` file.
Copy `.env.example` for a complete template.

## Scan settings

| Variable | Default | Description |
|---|---:|---|
| `RELEVANCE_THRESHOLD` | `0.7` | Minimum score included in a digest |
| `SCAN_INTERVAL_HOURS` | `6` | Delay between continuous-mode scans |
| `MAX_MESSAGES_PER_CHANNEL` | `200` | Maximum messages fetched per channel and scan |
| `SCAN_MAX_NEW_MESSAGES` | `300` | Maximum new messages evaluated after fetch and deduplication; `0` disables the cap |
| `KEYWORD_PREFILTER` | `true` | Drop messages that do not match project keywords before model evaluation |
| `SCOUT_ENVIRONMENT` | `development` | Provenance label: `development`, `test`, or `production` |
| `DB_PATH` | `scout.db` | Scout state database |
| `TRACE_DB_PATH` | `scout_traces.db` | Jig trace database |
| `FEEDBACK_DB_PATH` | `scout_feedback.db` | Jig feedback database |

## Platform limits

| Variable | Default | Description |
|---|---:|---|
| `FARCASTER_MAX_RESULTS_PER_QUERY` | `25` | Results fetched per search or channel request |
| `FARCASTER_MAX_PAGES` | `10` | Page ceiling per Farcaster request stream |
| `BLUESKY_MAX_RESULTS_PER_QUERY` | `25` | Results fetched per search or feed request |
| `BLUESKY_MAX_PAGES` | `10` | Page ceiling per Bluesky request stream |
| `BLUESKY_LANGS` | unset | Comma-separated language tags; missing post language metadata is retained |

Bluesky runs every built query against every configured language. Its maximum
keyword-search page count per scan is:

```text
number of queries × max(1, number of languages) × BLUESKY_MAX_PAGES
```

Each configured `BLUESKY_FEED_URIS` entry adds another paginated request
stream. Adding keywords or languages therefore increases rate-limit exposure
multiplicatively.

## Model routing

| Variable | Default | Description |
|---|---|---|
| `LLM_MODEL` | `claude-sonnet-4-6` | Backward-compatible default for all phases |
| `RELEVANCE_MODEL` | `LLM_MODEL` | High-volume relevance triage |
| `REPLY_DRAFT_MODEL` | `LLM_MODEL` | Reply drafting |
| `CRITIC_MODEL` | `LLM_MODEL` | Critique, rejection, and revision |

Model names pass directly to `jig.from_model`; Jig selects the backend from
the prefix:

- `claude-*` uses Anthropic and requires `ANTHROPIC_API_KEY`.
- `openrouter/<vendor>/<slug>` uses OpenRouter and requires
  `OPENROUTER_API_KEY`.
- `dispatch/<name>` uses the service configured by `DISPATCH_URL`.
- `ollama/<name>` uses a local Ollama server.

### Preflight model diversity

Preflight resolves model identity separately from routing. Its existing
pipeline-wide requirement is at least two distinct recognized model families
across relevance, reply drafting, and critique. This is a diversity check, not
proof of evaluator independence or a worker–critic-specific qualification gate.

Recognized OpenRouter namespace/family pairs are `anthropic/claude-*`,
`openai/gpt-*` (also `chatgpt-*`), `openai/o1`, `openai/o3`, `openai/o4`
(including their hyphenated variants, grouped as `o-series`),
`google/gemini-*`, `moonshotai/kimi-*`, and `qwen/qwen*` (numeric or hyphenated
version suffixes). For example, the published identifiers for
[Kimi K2](https://openrouter.ai/moonshotai/kimi-k2) and
[Qwen3](https://openrouter.ai/qwen/qwen3-235b-a22b) become
`openrouter/moonshotai/kimi-k2` and `openrouter/qwen/qwen3-235b-a22b` in Jig.

Direct Claude, GPT/ChatGPT, o-series, and Gemini identifiers resolve to the
same families as their OpenRouter counterparts. A different route, version,
or model size does not create another family. Recognition does not check API
availability, credentials, pricing, or behavioral qualification, and does not
change the models configured for any phase.

Unknown identifiers, mismatched developer namespaces, and opaque Dispatch or
Ollama aliases fail preflight with a diagnostic rather than count as another
family. Alias identity metadata is not yet supported; supporting an additional
identity requires a reviewed resolver mapping, not a made-up route prefix.
The report retains `model_families` and adds `model_identities` containing the
exact configured identifier, route, developer namespace, and family.

## Cost considerations

With keyword prefiltering enabled, a scan of 200 messages might send 10–30 to
the agent. Each candidate normally takes one or two model turns. Actual cost
depends on message length, dossier context, model choice, and provider pricing;
use provider usage reporting rather than treating a fixed estimate as a
budget.

Security-sensitive sidecar and web settings are documented separately in
[Deployment security](deployment-security.md).
