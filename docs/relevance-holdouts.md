# Relevance holdouts and the JEV handoff

Scout is to gain a second relevance classifier (JEV, answered by Typesafe System
One) and durable evidence for the decision it produces, so that a sampled holdout
can be exported for blind grading and released later against stored labels.

This document is the handoff record for that work. It states what Scout already
has, what the authoritative source outside Scout holds, which inputs are missing,
and which product choices are still open. Implementation of the authoritative
adapter, the router, and the holdout interchange stays blocked until the inputs
below are pinned and the choices below are confirmed.

## Status

Blocked, on inputs that are not Scout's to produce.

| Gate | State |
|---|---|
| Pinned assay revision for `route()`, `build_state()` and the catalogue loader | Candidate found, not confirmed as a pin (see [The authoritative source](#the-authoritative-source)) |
| The feature catalogue `route()` consumes | Private, outside Scout, must not be committed here |
| Redistributable synthetic fixture in the routed feature form | Does not exist yet; Scout carries an invented one (see [Fixtures](#fixtures)) |
| Request-path equivalence with the authoritative client | Unverified (see [The request path diverges](#the-request-path-diverges)) |
| Four product choices | Proposed below, awaiting owner and assay confirmation |
| Holdout export and label interchange | Proposed below, not written as a contract file |

Nothing in this document records a decision as settled. The proposals are written
so that a reviewer can accept or reject each one on its own.

## Repository topology

The base for this work was supplied by `provision_repository_refs`, not chosen
here:

- `repository_key`: `scout`
- base branch (what the pull request targets): `epic/jev-integration`
- integration branch (where the run's work merges later): `main`

At the time of writing `origin/epic/jev-integration` and the commit this work
branched from are the same commit, `83e0eac`. No branch was created, repaired or
renamed, no ancestry was inferred from the analysis revision, and no topology was
mutated. If the provisioned base moves, this section is the thing to re-check
before anything else in the document is trusted.

## What Scout has today

### A disabled shadow node

`src/scout/typesafe/` runs one non-gating relevance call per post when
`TYPESAFE_SHADOW_MODE=true`. It defaults to `false` and its only backend is
`placeholder` (`src/scout/config.py:165-180`). It stays disabled and unchanged by
this work.

The shadow node cannot stand in for the authoritative path. Its state projection
(`src/scout/typesafe/state.py`) emits

```text
post: id, platform, channel_name, content, created_at, url
author: name, handle
project: key, name, description, link
parent_context: object or null, with id/text/url/author
```

and the authoritative projection emits

```text
post: platform, channel, url, text
author: name, handle
project: key, name, description
parent_context_only: object or null, with author_name/text
```

Different key names, a different parent key, and a `project.link` field the
authoritative projection does not carry. Parity cannot be argued from the shadow
helpers; it has to be ported from the authoritative source.

The shadow catalogue model is also narrower than the authoritative one:
`src/scout/typesafe/catalogue.py` models `questions` as a list of objects with an
`id`, and models `state.parent_context_only` as a boolean. The authoritative
catalogue keys questions by name in a mapping and lists the parent fields it wants
under `parent_context_only`.

### A population export that already matches the loader's input

`PopulationExportRecord` (`src/scout/replay/population_export.py:20-38`) carries
exactly the fields the authoritative `build_state()` reads from a record:
`platform`, `channel`, `url`, `text`, `parent_author_name`, `parent_text`,
`author_name`, `author_handle`, alongside `evaluation_id`, `snapshot_id`,
`human_label`, `production_score` and `production_decision`. This is the one
interchange input that does not need to be invented. The holdout export should
extend it rather than replace it.

### Durable phase evidence

`evaluation_phase_runs` (`src/scout/storage/schema.py:916-961`) is the existing
link between a relevance call and its trace. A row is inserted only after the
tracer is flushed, the trace is read back, and the trace resolves to an
`AGENT_RUN` root (`_finalize_and_persist_phase_run` and `_verify_agent_run_root`,
`src/scout/scanning/pipeline.py:122-169`). The row is inserted unlinked and gets
its one permitted `evaluation_id` update in the same transaction as the
evaluation insert.

Two things follow for a direct HTTP classifier:

- `trace_id` is captured from the jig agent run today
  (`src/scout/scanning/pipeline.py:414-454`). A direct POST runs no agent, so a
  root has to be opened explicitly. Jig tracers expose
  `start_trace(name, metadata, kind=SpanKind.AGENT_RUN)`, so this is available,
  and the read-back verification then works unchanged.
- `snapshot_phase_id` is `NOT NULL` and references `feedback_snapshot_phases`,
  whose payload is the rendered feedback text injected into a phase prompt
  (`src/scout/storage/schema.py:821-834`). A JEV call injects no feedback text.
  The row still exists for every scan, so citing it is possible, but what it
  means for a JEV run is an open question listed below.

### An evaluation row with no classifier provenance

`evaluations` (`src/scout/storage/schema.py:386-412`) has no column naming the
classifier, model, catalogue or router that produced a decision, and
`surface_status` is constrained to `('surfaced', 'low_relevance', 'abstained',
'critic_rejected', 'gate_blocked', 'not_relevant', 'drafting_failed')`. `held` is
not in that list. Adding it, and adding provenance columns, is a migration whose
historical rows stay `unknown`/null. Nothing may be backfilled: no stored fact
identifies which classifier produced a pre-JEV evaluation.

### Routing

With `KEYWORD_PREFILTER=true` (the default, `src/scout/config.py:159`) a message
with no matching keyword is dropped before the relevance phase
(`src/scout/scanning/prefilter.py:138`), so every message that reaches relevance
carries a winning route and therefore a routed project. With
`KEYWORD_PREFILTER=false` the route may be `None` and `project_key` is `None`
(`src/scout/scanning/pipeline.py:181-190`). JEV's state projection requires a
project, so the unrouted case has no defined input. See semantics question 1.

## The authoritative source

### Where it is

| What | Where |
|---|---|
| Repository | `RankOneLabs/assay` |
| Path | `experiments/typesafe_relevance/` |
| Branch | `move-private-catalogues` |
| Commit | `282479cdc877d1d470840684e8cbea891f9c54d3` |
| Pushed | yes, `origin/move-private-catalogues` |
| Merged into `origin/main` | no |

Files that matter here, all present at that commit: `route.py` (the round 4
router), `state.py` (`build_state`), `catalogue.py` (the loader and
content-addressed version), `backends.py` (the Typesafe and OpenRouter IO
boundaries), `run_round4.py` (the wiring that proves how the pieces compose),
`tests/test_relevance_route.py` (seven route tests), and
`tests/fixtures/band-form.yaml` and `tests/fixtures/label-form.yaml` (two
made-up catalogues).

### Why this is a candidate and not yet a pin

Three reasons, each needing an answer from the owner of assay:

1. The commit sits on an unmerged branch. An unmerged branch can be rebased or
   dropped, and a Scout port that cites it would then cite nothing. Either the
   branch merges, or a tag is cut, or the owner states that this SHA is
   immutable and is the pin.
2. The branch is named for what it did: it moved the relevance catalogues out of
   assay. At this commit `experiments/typesafe_relevance/catalogues/` no longer
   exists. The router is there; the catalogue it routes over is not.
3. `route.py`'s own docstring cites `comms/relevance-jev-round-spec.md` as the
   written specification. `comms/` is gitignored in assay, so the specification
   is in no git history and cannot be pinned at all. Either it is published
   somewhere citable, or the router source itself becomes the specification of
   record and this document says so.

### What the router does

Stated here so that a reviewer can check the port without reading assay. Over one
post's feature probabilities, in order, stopping at the first match:

```text
any excl_* >= T_ex                                    -> drop
needs_thread >= T_t                                   -> review
answerable_from_post >= T_a and about_agent_work >= T_w -> respond
points_somewhere >= T_p                               -> review
otherwise                                             -> drop
```

and then one override: if any feature the path actually consulted is within a
margin `m` of its threshold, the outcome becomes `review`. The exclusion line
consults exactly one value, the largest `excl_*` probability, because that alone
decides whether "any" holds. Every answer whose name starts with `excl_` is an
exclusion, so the catalogue, not the router, decides which exclusions exist.

Every threshold defaults to `0.5` and the margin to `0.1`. Nothing is fitted.

The router returns `action`, `path_action` (the action before the margin
override), `line` (which line decided), `margin` (the consulted features that
were close), `exclusion` (the winning exclusion name, or null when the exclusion
line did not fire) and `features` (every probability it read). Persisting all
of it is what lets a stored decision be explained later.

An answer is read as `answers[name]["noul"]`, a number. A missing name, a
non-mapping value, or a missing or non-numeric `noul` raises rather than
defaulting. There is no partial answer vector: a short or malformed reply is a
failure, never a silent negative.

### What the catalogue holds

The catalogue `route()` consumes is `agent-ops-relevance-features.v6.yaml`. It is
private and lives in the run-receipts repository. It must not be committed to
Scout, quoted in Scout, or reproduced in Scout's tests.

What can be said about its shape without reproducing it: it declares the same
five top-level keys the loader requires (`id`, `decide`, `description`, `state`,
`questions`), its `decide` is `agent_ops_route/v1`, its state projection is the
authoritative projection shown above, and its `questions` mapping holds seven
`excl_*` questions plus the four routed features `answerable_from_post`,
`about_agent_work`, `points_somewhere` and `needs_thread`. Every one of the
eleven is `type: noul` with a `criteria` mapping keyed by the two literal strings
`true` and `false`.

That last detail is load-bearing. YAML 1.1 resolves bare `true` and `false` to
booleans, so a plain `yaml.safe_load` turns those criteria keys into Python
`True`/`False` and the questions mapping sent to the classifier stops matching
what the authoritative runs sent. The authoritative loader subclasses
`yaml.SafeLoader` and removes `tag:yaml.org,2002:bool` from every implicit
resolver so the keys stay strings. Scout's port has to do the same, and a test
has to hold it.

The questions mapping is passed to the classifier verbatim, exactly as loaded
(`run_round4.py` calls the backend with `catalogue.questions`). Scout must not
re-render, re-key or normalise it.

### The request path diverges

The authoritative backend calls the Typesafe SDK:

```python
with TypeSafeClient(api_key=..., model="jev-latest",
                    retry=RetryPolicy(max_retries=0), timeout=60.0) as client:
    response = client.system_one(state=state, questions=questions)
```

The approved Scout decision is a direct bearer-authenticated `httpx` POST to
`/v1/systemone` on the configured base URL (default `https://api.typesafe.ai`),
model `jev-latest`, 60-second timeout, no retry inside the adapter.

The two are the same intent, and the direct POST is the decided path. What is not
established is that they put the same bytes on the wire. The SDK's serialisation
of `state` and `questions`, its request headers, and the shape it accepts back
are not pinned anywhere in assay; they live inside `typesafe_sdk`. Until the
request and response wire format is pinned (a captured request/response pair from
an authoritative run would do it, with credentials stripped), a Scout adapter can
be tested for its own behaviour but not for equivalence with the runs the
published numbers came from.

This is a parity gate on the adapter, separate from the pin gate on the router.

## Missing inputs

| Input | Should come from | State | Blocks |
|---|---|---|---|
| Pinned revision of `route()` and its tests | assay owner | candidate identified, unconfirmed | the router port, parity tests |
| Pinned `build_state()` and catalogue loader | same revision | same | the state projection, the loader |
| The routed feature catalogue | private, run-receipts | present but not redistributable | any end-to-end fixture |
| Written round specification | `comms/relevance-jev-round-spec.md`, gitignored | not citable | the contract document |
| Typesafe wire format for `/v1/systemone` | Typesafe SDK or a captured exchange | not pinned | adapter parity |
| Holdout export and label interchange | agreement with assay | proposed below, unconfirmed | C02 |

## Semantics awaiting confirmation

Four product choices are unresolved in the approved plan. Each is stated as a
question, with the proposed resolution, so it can be accepted or rejected on its
own. None of them is implemented.

### 1. What does JEV do with an unrouted post?

JEV's state projection requires a project, and a post that arrives with no
keyword route has none.

**Proposed:** JEV requires `KEYWORD_PREFILTER=true`. Configuration validation
fails at startup when the classifier is JEV and the prefilter is off. An
unexpected unrouted input reaching the JEV adapter fails retryably before any
HTTP request is made, leaving the post unevaluated. The LLM classifier keeps its
existing unrouted behaviour unchanged.

**Why it needs an answer:** it removes a supported configuration for JEV runs,
which is a product choice, not an implementation detail.

### 2. What numeric score does a JEV decision carry?

Scout's evaluation row requires a `score`. JEV produces an action, not a
calibrated probability of relevance.

**Proposed:** `respond` and `review` record `score = 1.0`, `drop` records
`score = 0.0`, as compatibility values only. These are explicitly not calibrated
confidence and nothing downstream may read them as one. The routing action, not
the score, controls JEV relevance.

**Why it needs an answer:** these values will be stored and later exported. If
anyone intends to compare scores across classifiers, the mapping has to be
rejected now rather than discovered later.

### 3. What action does an LLM decision record?

Sampling and release need a stable action for every decision, including the LLM
ones that predate JEV.

**Proposed:** record `respond` when the LLM says relevant and its score is at or
above the `RELEVANCE_THRESHOLD` in force at decision time, otherwise `drop`. The
LLM never records `review`. The existing non-held LLM flow is unchanged, and an
ungraded release uses the recorded action rather than recomputing a threshold
later.

**Why it needs an answer:** it fixes what a low-score LLM decision means for
release, and it makes the recorded action, not a later threshold, the thing
release acts on.

### 4. What is the holdout interchange?

Blind grading needs an export Scout produces and labels Scout reads back. Neither
exists.

**Proposed:** a versioned export that carries the existing population fields,
plus holdout, evaluation, post and project identity, plus a clearly separated
private envelope holding the production decision that a blind reviewer must not
see. Labels join back on stable identity and the frozen project, never on row
order and never on URL alone. Label precedence, when the same case is labelled
more than once, is decided by assay and recorded here.

**Why it needs an answer:** the format is an agreement between two repositories.
Inventing it in Scout produces a packet assay cannot read.

A sketch of the proposal, to argue against rather than to implement:

```text
export record  = PopulationExportRecord fields
               + holdout_id, evaluation_id, post_id, project_key
               + frozen project identity (key, name, description)
               + private: { production_score, production_decision, action, reason }

label record   = holdout_id, evaluation_id, label, labeller, labelled_at
               + provenance: source packet identity and revision
```

The `private` envelope is named so that a blind projection can drop one key and
be checkable, in the way the authoritative packet builder projects a blind case
and then asserts that nothing on its denylist survived.

The contract files `contracts/relevance/holdout-export.v1.schema.json` and
`contracts/relevance/holdout-labels.v1.schema.json` are deliberately not written
yet. A schema file in `contracts/` reads as settled, and this is not.

## Fixtures

The two made-up catalogues at the candidate assay revision are redistributable,
and neither is in the form `route()` consumes:

- `band-form.yaml` is the band-era catalogue: an exclusion choice question, the
  substance features the band mapping reads, and a four-level band score. It
  feeds `mappings.decide_argmax`, not `route`.
- `label-form.yaml` is the human label form: exclusion, substance, and a
  `needs_thread` flag. It is a form for people, not a feature catalogue for the
  classifier.

So there is no redistributable fixture in the routed feature form. Scout carries
an invented one instead, at `tests/fixtures/relevance/`:

- `routed-features.fixture.yaml`, a made-up catalogue in the authoritative routed
  shape: the four routed `noul` features, three invented exclusions, literal
  `true`/`false` criteria keys, and the authoritative state projection. Every
  word of its content is invented. It is not the real catalogue and produces no
  real decision.
- `routed-answers.fixture.json`, one answer vector in the shape the router reads.

`tests/test_jev_parity.py` holds these to the shape the port has to satisfy: the
routed feature names, the `excl_` prefix rule, `noul` types, the state projection
keys, and the criteria keys surviving as strings. Those are shape assertions. They
are not parity: parity needs the pinned source and the ported route tests, and
stays blocked.

When the authoritative revision is pinned, the seven route tests at that revision
port across unchanged in substance, because they use invented exclusion names
already and exercise respond, review, drop, exclusion precedence, the
`answerable and about` conjunction, and the margin override including the case
where an unconsulted feature sits on its threshold and must be ignored.

## What stays blocked

Until the inputs are pinned and the four questions are answered:

- the JEV adapter, router, catalogue loader and state projection
  (`src/scout/scanning/jev*.py`)
- the holdout export and label contracts under `contracts/relevance/`
- classifier selection in the scanning pipeline, and the evidence and storage
  work that depends on the chosen semantics

C02 depends on the storage and claim primitives this cohort was to supply, and on
the interchange in question 4. It is blocked on the same gates, and Scout's merge
stays gated on the source and contract handoff resolving.

What is not blocked, and is not waiting on anyone: this document, the invented
fixtures and the shape tests over them.
