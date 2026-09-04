# Architecture Rationale

## Core Principle: Composable, Traceable, Visualizable Flows

Every convention in CLAUDE.md serves one architectural goal: **all logic should be expressible as composable pipelines whose execution can be traced and visually represented as DAGs**.

This is not a style preference. It's an architectural decision driven by how the user works — they spend the majority of their time in visual representations of flows.

## Why Functional Pipelines

1. **Composability** — small named transforms snap together; adding a stage doesn't require rewriting the pipeline
2. **Flow tracking / debugging** — each transform has a clear input, output, and failure mode; you can trace data through the pipeline step by step
3. **Visual representation** — pipelines map directly to DAGs; each node is a transform, each edge is data flowing between transforms

## Why Result Types (Ok/Err)

Ok/Err is **strictly pipeline plumbing** — a universal interface contract that makes transforms composable with a standard error handling pattern regardless of position in the stack.

Think of it as the standard connector between any two pipeline stages. Every transform either produces a value (Ok) or an error (Err), and the next stage can pattern-match on that.

## Why Domain Error Types with Context

In agent loops with various iterations, something could be erroring and you can see the error but not know how it got there. Pipelines with Result types enable **standardized tracing**:

- Every Err carries operation name, entity ID, and detail
- You can see exactly which transform failed, on which input, with what context
- The granularity is controllable — add more context fields to error types as needed
- This enables building trace/observability tooling that works across all pipelines uniformly

## Fractal Nesting

Pipelines are fractal. A node in a pipeline can itself be a pipeline — cycles, sub-agents, retry loops are all subgraphs that nest inside the parent as a single node.

- **Zoom out**: the node has a typed boundary (`Result[T, E]`), same as any other transform
- **Zoom in**: it expands into its own DAG (possibly with cycles/conditional back-edges)
- **Nests indefinitely**: an agent calling another agent is a node whose implementation is another graph
- **Traces compose**: outer trace shows "node X failed," expanding X shows the inner trace with the specific stage and input that failed
- **Same contract at every level**: `Result[T, E]` at the boundary whether it's a pure function or a 50-step sub-agent

Example in engagement-scout: the per-message flow is a `scout_message` pipeline whose one `score_and_draft` node expands into a `jig.run_agent` subgraph. Zoomed out it's `message in → ReplyCandidate out`. Zoomed in it's the agent loop — evaluate, draft, self-critique, optionally call `revise_draft` and iterate, then `submit_output`.

SysVista implication: any node should be expandable/collapsible in the viewer. The flow view / system view toggle is the first version of this zoom. Generalize to arbitrary nesting depth.

## Key Invariant

If you can't draw the data flow as a DAG where each node is a pure-ish transform and each edge is typed data (including Result), the design needs rethinking. This applies recursively — every subgraph must satisfy the same invariant.

## Example: classify_outcome as a pure boundary transform

`scan_runner.classify_outcome` is the node that turns a scored `ReplyCandidate` into a terminal `OutcomeDecision` — synchronous, DB-free, with no logging or clock access. Its only collaborators are `RELEVANCE_THRESHOLD`, immutable input objects, and `verify_draft_content`. `persist_outcome` is the adjacent, separate node that writes an `OutcomeDecision` through `StateManager`; keeping classification and persistence as distinct typed-boundary nodes is what makes the full status ladder (surfaced, abstained, critic_rejected, gate_blocked, not_relevant, low_relevance, drafting_failed) independently testable without SQLite.

`evaluations.abstain_reason` is a deprecated compatibility column from an earlier terminal-reason shape. The active contract is `evaluations.failure_reason`, populated uniformly for every non-surfaced outcome (including `abstained`, where it holds `StructuredDraftOutput.abstain_reason`). `abstain_reason` is read by no code path and written by none going forward; it remains only for historical rows already classified by migration 17.

## PAA task declarations as a typed evaluator-identity boundary

Scout takes the PAA contract and its reference control plane from
[RankOneLabs/paa](https://github.com/RankOneLabs/paa) as pinned
dependencies (`paa-contracts`, `paa-runtime`) and checks in three
declarations at `contracts/paa/*.v1.yaml` (`inbound_reply_surfacing`
shadow/hitl, `canonical_promotion` disabled/hitl, `reply_draft`
shadow/manual). All three are visible to the `scout paa` control plane.
The replay-only `reply_draft` task has no reachable transition from its
initial manual position; activation requires a new declaration. Scout no longer
implements the loader: `paa_runtime.declarations` does, and
`scout.paa.declarations` binds it to Scout's declarations directory and
producer registry. Schema conformance is proved by
`tests/test_paa_declarations.py` against the packaged artifacts — no
checkout, no env var, no skip path.

That leaves the registry as Scout's own job: proving every declared
evaluator resolves to a real (or explicitly reserved future) Scout
producer.

That resolution is the boundary this doc's "typed edge" principle applies
to at the evaluator-identity level: each evaluator's (property, target,
technique, evaluation_basis, epistemic_status, version, authority) tuple
is the typed contract between a PAA declaration and the Scout code that
actually produces its verdict. `evaluation_basis` and `epistemic_status`
were a single `oracle` field until this contract revision; one field could
not distinguish a rubric-graded proxy from a rubric-graded ground truth,
which collapsed two genuinely different producers of the same property
into one identity. `scout.paa.declarations.PRODUCER_REGISTRY` is the one place that boundary is
checked — `CONTENT_INVARIANTS_EVALUATOR_VERSION`, `AUTHOR_RATE_EVALUATOR_VERSION`,
`LLM_CRITIC_PROMPT_VERSION`, and `HUMAN_GRADE_SCHEMA_VERSION` each live
beside the verdict-producing code they version, not inside the declaration
or the loader, so a version bump and the code change it describes can
never drift apart. Three evaluators (`draft_quality`, `claim_admissibility`,
and `canonical_truth`) remain explicitly reserved future producers;
canonical-promotion runtime remains deferred. A version bump to any
producer constant and its PAA declaration reference must land together,
the same way a pipeline node's `Result[T, E]` boundary and its caller must
agree on shape at every level of nesting.

## The PAA autonomy control plane as event-sourced state

Building on cohort 1's declaration loader, this cohort adds the append-only
`autonomy_events` table (added v23, reshaped v33), `paa_runtime.events`'
typed event/position/status vocabulary, `paa_runtime.evidence`'s
content-addressed evidence store, and `paa_runtime.service`'s
propose/approve/reject/demote/resolve/show/
list workflows, exposed through `scout paa`. This is the same "typed edge,
fractal nesting" discipline the rest of this doc describes, applied to
autonomy state: **current position is a pure fold over event history, never
a stored field.** `paa_runtime.service.resolve_current_position` is the one
function that performs that fold — it loads the task's declaration, reads
the latest `position_changed` event whose `(task, declaration_version,
scope)` triple matches exactly, and returns that event's `to_position` or,
absent any such event, the declaration's own `initial_position`. Nothing
else in the codebase is permitted to cache or shortcut that result; there
is deliberately no `current_position` column anywhere.

Exact-scope isolation and declaration-version reset are both consequences
of treating `(task, declaration_version, scope)` as one opaque compound
key rather than a hierarchy: two different scope strings under the same
task are fully independent — position earned under one never leaks to the
other — and a declaration version bump resets a task to its new
`initial_position` rather than inheriting a prior version's earned
position — the same "no wildcard, no parent/child, no fallback" boundary
discipline `PRODUCER_REGISTRY`'s exact-tuple matching already applies to
evaluator identity, applied here to autonomy authority.

`paa_runtime.service.approve` is the one place a position actually changes, and it
is the clearest illustration of this cohort's core invariant: authorization
and effect commit together or not at all. It runs inside one
`Db.begin_immediate()` transaction that re-verifies the proposal's evidence
bytes against their recorded hash, reloads the declaration fresh (failing
if its version has drifted since the proposal), re-checks the transition
against that declaration's exact promotion or demotion, and compares the
proposal's exact position-event baseline with the latest event under the
lock. Any intervening change makes the proposal stale even if later events
cycle the scope back to the same position — only then does it insert
`motion_approved` and `position_changed` together. Every event type carries
non-null transition, evidence, actor, and reason identity; rejection copies
the proposal identity rather than storing a partial row. Idempotency is
deliberately narrow: a
retry is safe only when both sibling events already exist and exactly
match the proposal; any partial or contradictory combination (one event
without its sibling, or a rejection alongside an approval) is surfaced as
corrupt history rather than silently resolved, mirroring this doc's
insistence elsewhere that a pipeline's failure mode stay legible rather
than papered over. See `docs/runbooks/paa-operations.md` for the full
operator procedure and `paa_runtime.service`'s module docstring for the
complete state-machine contract.
