# Evaluations

Scout's Phase 1 evaluation system lives in `src/scout/evals/phase1/`. It runs
the checked-in YAML corpus through the production relevance, drafting, critic,
and dossier-safety paths without granting write access to a dossier source.

## Production evidence

The Phase 1 exit audit accepts fixed UTC half-open boundaries and emits a
schema-v3 canonical JSON report plus Markdown rendered from that same object.
Only `production` and `live` scans qualify. Historical drafts are replayed
through the pure content verifier, so current author-rate rows cannot alter a
historical outcome. Approval references and tamper-evident bundles contain no
deployment-specific procedure.

## Corpus execution

`scout.evals.phase1.loader.load_phase1_corpus` validates case shape,
distribution, the pinned dossier, stored grades, and unique IDs before
constructing an immutable `Phase1Registry`.

`ScoutPipelineAdapter` runs `scout_response` cases through the real pipeline.
It sends `dossier_bad_write` cases to a pure read-only rail evaluator, which
reports `automated_write_forbidden` and never performs the proposed operation.

`ScoutPhase1Grader` scores three binary dimensions:

- `outcome_semantics` checks the exact terminal status and applicable posture.
- `content_safety` reruns draft verification or checks the expected rail
  violations.
- `grade_contract` validates the stored grade against the shared schema.

The export adapter projects eligible production grades into the same Jig case
shape and a seven-dimension human score vector. It is pure and read-only;
Scout's tables remain the mutable authority.

## Hermetic and live runs

CI scripts all inner model phases and uses generalized fixture dossiers in a
temporary git repository. It makes no paid model calls. A seeded regression
confirms that Jig's regression detection notices a known score drop.

A live model sweep is operator-only and never runs in CI:

```bash
uv run scout eval phase1 \
  --configs eval-phase1-sweep.yaml \
  --dossier-root /path/to/dossier-source \
  --output phase1_sweep_result.json
```

Example configuration:

```yaml
concurrency: 4
seeds: 1
threshold: 0.05
baseline:
  name: baseline
  relevance_model: claude-sonnet-4-6
  reply_draft_model: claude-sonnet-4-6
  critic_model: claude-sonnet-4-6
candidates:
  - name: candidate-haiku-draft
    relevance_model: claude-sonnet-4-6
    reply_draft_model: claude-haiku-4-6
    critic_model: claude-sonnet-4-6
```

The command prints rollups and regression alerts, optionally writes JSON, and
exits nonzero when a run fails or a regression crosses the configured
threshold.

For replaying recorded production phases against candidate prompts or models,
see [Offline replay](operations/offline-replay.md).
