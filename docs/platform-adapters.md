# Platform adapters

Scout currently reads Discord, Farcaster, and Bluesky. All adapters normalize
source records into `scout.config.Message`, allowing prefiltering, evaluation,
storage, and digest generation to remain platform-independent.

Bluesky replies include immediate-parent context when
`app.bsky.feed.getPosts` can retrieve it. A compact unavailable marker tells
the model when that lookup fails. Farcaster parent-context retrieval is not
currently implemented.

## Adding a platform

1. Add an adapter under `src/scout/platforms/` that returns `list[Message]`.
2. Set a stable `platform` value on every message.
3. Add the adapter to `src/scout/scanning/runner.py` alongside the existing
   sources.
4. Add deduplication, pagination, error, and orchestration tests.
5. Document credentials and request-volume controls in
   [Configuration](configuration.md).

Adapters are responsible for fetching and normalizing source data. They must
not bypass the shared scanner, dossier-readiness checks, state manager, or
grading paths.
