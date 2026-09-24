# Relevance holdouts and the JEV handoff

Scout gains a second relevance classifier (JEV, answered by Typesafe System One)
and durable evidence for the decision it produces, so a sampled holdout can be
exported for blind grading and released later against stored labels.

This document is the contract record for that work. It records the proposed
semantics, request, storage, and Scout/assay interchange for owner review.
The four product choices and the exact packet, label, and private answer-key
contract still need explicit confirmation from the project owner and assay.
Dependent implementation and merging remain blocked until that confirmation is
recorded here.

## Confirmation status

**Pending owner and assay confirmation.** The cutover specification proposes
the following four choices; it is not evidence that the owner accepted them:

1. JEV requires keyword prefiltering. An unexpected unrouted post fails
   retryably before the HTTP request, while LLM routing keeps its current behavior.
2. JEV stores compatibility scores of 1.0 for `respond` and `review`, and 0.0
   for `drop`. These values are not calibrated confidence.
3. LLM stores `respond` only when relevant and at or above the threshold in
   force at decision time; otherwise it stores `drop`. It never stores `review`.
4. Holdout export extends the population record with stable holdout,
   evaluation, post, and frozen project identity; a private envelope isolates
   the production decision. Labels must join on stable identity, not row order
   or URL. The precise packet, label, and private answer-key fields and label
   precedence are pending agreement with assay.

The two schemas under `contracts/relevance/` are **proposals**, not accepted
interchange contracts. In particular, the current label schema has no frozen
project identity, and there is no answer-key schema. Both gaps need resolution
before export or release implementation. The proposed label precedence is last
by `labelled_at`, rejecting ties; assay has not confirmed it. No private
catalogue or private packet belongs in this repository.

## Where the decisions come from

`comms/jev-cutover-spec.md` (2026-09-23) is a local proposal for this work.
It is gitignored in Scout and cannot serve as a reviewable owner approval.

`route()`, `build_state()` and the catalogue loader are ported from
`RankOneLabs/assay`, `experiments/typesafe_relevance/`, at commit
`282479cdc877d1d470840684e8cbea891f9c54d3`. Two provenance notes a reader should
carry:

- That commit is on `move-private-catalogues`, which is pushed but not merged
  into `origin/main`. The SHA is immutable and is the pin; the branch label is
  not. If the branch is ever dropped and the object collected, the port here
  becomes the only surviving copy.
- `route.py`'s docstring cites `comms/relevance-jev-round-spec.md` as its
  written specification. That path is gitignored in assay and is in no git
  history, so the router source at the pinned SHA, and the restatement below,
  are the specification of record.

## Repository topology

The base for this work was supplied by `provision_repository_refs`, not chosen
here:

- `repository_key`: `scout`
- base branch (what the pull request targets): `epic/jev-integration`
- integration branch (where the run's work merges later): `main`

At the time of writing `origin/epic/jev-integration` and the commit this work
branched from are the same commit, `83e0eac`. No branch was created, repaired or
renamed, no ancestry was inferred from the analysis revision, and no topology was
mutated.

## The classifier

`RELEVANCE_CLASSIFIER` accepts `llm` or `jev` and defaults to `llm`. Exactly one
runs. `llm` is the rollback path and stays until JEV has held up in production.
There is no dual execution, no fallback from one to the other, and no
experimenting inside Scout: classifier and prompt changes are proven in assay.

Under `jev` the relevance phase builds the state and questions, calls JEV, runs
`route()`, and returns the same `RelevancePhaseOutput` the LLM phase returns.
Everything after the relevance phase reads only that value, so drafting, the
critic, the verifier gates and grading are unchanged.

| `route()` action | `relevant` | `score` | continues to drafting |
|---|---|---|---|
| `respond` | true | 1.0 | yes |
| `review` | true | 1.0 | yes |
| `drop` | false | 0.0 | no |

`reason` is the line that decided it, and the exclusion when one fired.
`relevant_to` is the routed project, as a single-element list.

The scores are compatibility values for a column that requires a number. They
are not calibrated confidence and nothing downstream may read them as one. The
routing action is the decision. `classify_outcome` does not apply
`RELEVANCE_THRESHOLD` to a JEV decision for that reason; every other gate
(critic, abstention, verifier, author rate, account blocks) applies unchanged.

`review` is carried through drafting exactly as `respond` is, and survives
persistence as a recorded action, so a reviewer can tell the two apart on an
evaluation that looks otherwise identical.

A JEV call failure is a relevance failure and is handled as an LLM relevance
failure is today: the post is preserved as unevaluated and stays retryable. No
evaluation row is written for a failed attempt.

### The routed project is required

JEV's state carries a project, and the project comes from the keyword route.
With `KEYWORD_PREFILTER=true` (the default) a post with no matching keyword is
dropped before the relevance phase, so every post reaching JEV carries a winning
route. With the prefilter off the route can be absent and there is no defined
input.

`RELEVANCE_CLASSIFIER=jev` therefore requires `KEYWORD_PREFILTER=true`;
configuration validation fails at startup otherwise. An unrouted post that
reaches the JEV adapter anyway fails retryably before any HTTP request is made.
The LLM classifier keeps its existing unrouted behaviour unchanged.

## The request

JEV was graded on this exact request, so the request is a contract, not an
implementation detail.

```text
POST {TYPESAFE_BASE_URL or https://api.typesafe.ai}/v1/systemone
Authorization: Bearer $TYPESAFE_API_KEY
Content-Type: application/json

{"state": <state>, "model": "jev-latest", "questions": <questions>}
```

One request, 60-second timeout, no retry inside the adapter, `httpx` directly
with no Typesafe SDK import. A failed attempt stays visible and retryable rather
than being papered over by a second call or by the other classifier.

The bearer credential is never stored on an evaluation, never written to a
decision record, and never logged.

### State

```text
post:                {platform, channel, url, text}
parent_context_only: {author_name, text} | null
author:              {name, handle}
project:             {key, name, description}
```

`channel` comes from the message's `channel_name` and `text` from its `content`.
The parent comes from `parent.author.name` and `parent.text`, and the whole
`parent_context_only` value is null when there is no parent. A field the message
does not carry is projected as null rather than omitted or defaulted to a string.

This is the projection `build_state()` performs at the pinned revision. It is
not the projection Scout's disabled shadow node performs; see
[the shadow node](#the-disabled-shadow-node) below.

### Questions

The v6 catalogue's `questions` mapping, passed verbatim as loaded. Scout does
not re-render, re-key or normalise it.

The loader keeps the `true`/`false` criteria keys as strings. YAML 1.1 resolves
bare `true` and `false` to booleans, so a plain `yaml.safe_load` turns those keys
into Python `True`/`False` and the mapping sent to the classifier stops matching
what the graded runs sent. The loader subclasses `yaml.SafeLoader` and removes
`tag:yaml.org,2002:bool` from every implicit resolver. A test holds this.

### Answers

Each answer is `{"type": "noul", "noul": p}` with `p` a number. A missing
question, a non-mapping answer, a missing or non-numeric `noul`, or a `type`
other than `noul` is a failure. There is no partial answer vector and no silent
negative: an incomplete reply is rejected before `route()` is called.

## The router

Restated here so a reviewer can check the port without reading assay. Over one
post's feature probabilities, in order, stopping at the first match:

```text
any excl_* >= T_ex                                      -> drop
needs_thread >= T_t                                     -> review
answerable_from_post >= T_a and about_agent_work >= T_w  -> respond
points_somewhere >= T_p                                 -> review
otherwise                                               -> drop
```

Then one override: if any feature the path actually consulted is within a margin
`m` of its threshold, the outcome becomes `review`. The exclusion line consults
exactly one value, the largest `excl_*` probability, because that alone decides
whether "any" holds. A feature the path never consulted is never close enough to
matter, even when it sits exactly on its threshold.

Every answer whose name starts with `excl_` is an exclusion, so the catalogue,
not the router, decides which exclusions exist. An answer vector with no `excl_*`
member is rejected.

Every threshold defaults to `0.5` and the margin to `0.1`. Nothing is fitted.

The router returns `action`, `path_action` (the action before the margin
override), `line` (which line decided), `margin` (the consulted features that
were close), `exclusion` (the winning exclusion name, or null when the exclusion
line did not fire) and `features` (every probability it read). All of it is
persisted, which is what lets a stored decision be explained later.

## The catalogue stays private

The catalogue `route()` consumes is `agent-ops-relevance-features.v6.yaml`. It
lives in the run-receipts repository and is read through
`TYPESAFE_CATALOGUE_PATH`. Scout is a public repository: v6 must not be
committed here, quoted here, or reproduced in Scout's tests.

What can be said about its shape without reproducing it: it declares the five
top-level keys the loader requires (`id`, `decide`, `description`, `state`,
`questions`), its state projection is the one above, and its `questions` mapping
holds seven `excl_*` questions plus the four routed features
`answerable_from_post`, `about_agent_work`, `points_somewhere` and
`needs_thread`. All eleven are `type: noul` with a `criteria` mapping keyed by
the two literal strings `true` and `false`.

Scout's tests use an invented catalogue in that shape. See [Fixtures](#fixtures).

### The pre-enable check

Parity with the graded runs is established once, operationally, before
`RELEVANCE_CLASSIFIER=jev` is enabled: on otto, with the real v6, check that
Scout sends the same `questions` as assay's loader produces and projects a round
5 export record to the same state as assay's `build_state`.

This is the parity gate. It cannot be a committed fixture, because the catalogue
it needs is private and the round 5 records are private evidence. Nothing in
Scout's test suite asserts parity; the tests assert the port's own behaviour and
the shapes the port must satisfy.

## Holdouts

`RELEVANCE_HOLDOUT_RATE` defaults to `0.1`. After the classifier decides, a post
is held with that probability whatever the action, drops included.

A held post gets its evaluation with the recorded action and
`surface_status = held`, and stops there: no draft, no surfacing. `held` is a
lifecycle state of its own. It is not surfaced, and it is not ready for
drafting; consumers of `surface_status` must not fold it into either.

A `relevance_holdouts` row records the hold. Release uses stored labels where
they exist:

| label | released action |
|---|---|
| `exclusion` | drop |
| `in_post` | respond |
| `pointer` | review |
| `none` | drop |
| ungraded | the recorded classifier action |

`respond` and `review` continue through drafting and the critic on the existing
human-relevance path. `drop` ends there. Release does not recompute a threshold:
it acts on the recorded action.

Sampling, the export command and the release command are C02's. This cohort
supplies the storage and the claim primitives they run on, and the interchange
both sides read.

### Holdout storage

A hold references an immutable source evaluation. A released outcome that drafts
gets a distinct target evaluation, uniquely linked back to the hold, so the
original decision survives release rather than being overwritten.

The current row carries `held_at`, `released_at`, the claim token and lease, the
release authority and value, a label source string, target identity, attempt
and error state, and the frozen post, parent, author, project, route and dossier
identity. Structured packet, label, and key provenance is still required before
release can use this storage. Freezing the input identity lets a label join to
the case it was written for once the interchange is agreed.

Claims are compare-and-swap with a fence. A stale or competing completion is
rejected; a committed completion is idempotently readable, so a retry after a
lost response reads the first result rather than producing a second one. One
source evaluation cannot acquire two holds, and cannot acquire two release
targets.

## The interchange

Two versioned contracts under `contracts/relevance/`:

- `holdout-export.v1.schema.json` — what `scout holdout export` writes.
- `holdout-labels.v1.schema.json` — what `scout holdout release --labels` reads.

The export is the population export format assay already reads, extended. Every
`PopulationExportRecord` field is present and unchanged, so an assay packet
builder that reads the population format reads this. Added on top: holdout,
evaluation, post and project identity, the frozen project the decision was made
against, and a `private` envelope.

The `private` envelope holds the production decision a blind reviewer must not
see: the recorded action, the score, the reason, and the classifier provenance.
It is one named key so a blind projection can drop it and be checked, in the way
assay's packet builder projects a blind case and then asserts nothing on its
denylist survived.

The proposed join uses `holdout_id` and `evaluation_id` against the frozen
project, never row order or URL alone. The current label schema lacks a project
field, so this proposed check cannot yet be implemented. The proposed duplicate
rule takes the last `labelled_at` and rejects ties. Assay must confirm these
rules and the packet and answer-key fields before either schema is treated as
an interchange contract.

## What Scout already had

### The disabled shadow node

`src/scout/typesafe/` runs one non-gating relevance call per post when
`TYPESAFE_SHADOW_MODE=true`. It defaults to `false`, its only backend is
`placeholder`, and it stays disabled and unchanged by this work. Removing it is
separate cleanup.

It is not a starting point for the JEV path. Its state projection emits

```text
post: id, platform, channel_name, content, created_at, url
author: name, handle
project: key, name, description, link
parent_context: object or null, with id/text/url/author
```

against the authoritative projection's `post: platform, channel, url, text`,
`parent_context_only: {author_name, text}` and a project with no `link`.
Different key names, a different parent key, and a field the authoritative
projection does not carry.

Its catalogue model is narrower too: it models `questions` as a list of objects
with an `id` and `state.parent_context_only` as a boolean, where the
authoritative catalogue keys questions by name in a mapping and lists the parent
fields under `parent_context_only`. It also validates `decide` against a
registered set the v6 catalogue is not in.

The JEV path therefore has its own loader, projection and router in
`src/scout/scanning/jev_*.py`. The shadow modules are untouched.

### A population export that already matches the loader's input

`PopulationExportRecord` (`src/scout/replay/population_export.py`) carries
exactly the fields the authoritative `build_state()` reads from a record:
`platform`, `channel`, `url`, `text`, `parent_author_name`, `parent_text`,
`author_name`, `author_handle`, alongside `evaluation_id`, `snapshot_id`,
`human_label`, `production_score` and `production_decision`. The holdout export
extends it rather than replacing it.

### Durable phase evidence

`evaluation_phase_runs` is the existing link between a relevance call and its
trace. A row is inserted only after the tracer is flushed, the trace is read
back, and the trace resolves to an `AGENT_RUN` root. The row is inserted unlinked
and gets its one permitted `evaluation_id` update in the same transaction as the
evaluation insert.

A direct HTTP classifier runs no jig agent, so it opens the `AGENT_RUN` root
itself through the tracer's `start_trace`, and wraps the HTTP call and the
routing in it. The same flush, read-back and root verification then runs
unchanged. If that evidence cannot be verified, no phase run is inserted and no
evaluation is claimed: an absent row is more truthful than one pointing at
evidence that is not there. A failed JEV attempt still records its failed phase
run, without evaluating the post.

`snapshot_phase_id` is `NOT NULL` and references `feedback_snapshot_phases`,
whose payload is rendered feedback text injected into a phase prompt. A JEV call
injects no feedback text. The row exists for every scan and the JEV run cites the
scan's relevance snapshot phase. For JEV this reference records only the scan's
snapshot identity at decision time; it does not show that JEV received, read, or
used the snapshot payload. Consumers must not interpret it as prompt feedback
provenance for JEV.

### Classifier provenance

`evaluations` had no column naming what produced a decision. It now records the
classifier, the model, the catalogue id and version, and the router version, plus
the action and the reason, with the full validated answers and the complete
`route()` decision, including the deciding line and the exclusion, stored beside
it.

Historical rows stay null. No stored fact identifies which classifier produced a
pre-JEV evaluation, so nothing is backfilled and nothing is guessed. A null there
means unknown, and reads as unknown.

## Fixtures

Neither redistributable catalogue at the pinned assay revision is in the form
`route()` consumes: `band-form.yaml` is the band-era catalogue feeding
`mappings.decide_argmax`, and `label-form.yaml` is the human label form. Scout
currently carries an invented candidate at `tests/fixtures/relevance/`. The
project owner and assay still need to agree that it is the synthetic fixture
for response and interchange tests:

- `routed-features.fixture.yaml`, an invented catalogue in the authoritative
  routed shape: the four routed `noul` features, three invented exclusions,
  literal `true`/`false` criteria keys, and the authoritative state projection.
  Every word of its content is invented, including the exclusion names. It is not
  the real catalogue and produces no real decision.
- `routed-answers.fixture.json`, seven answer vectors in the shape the router
  reads, one per routing outcome: respond, review on `needs_thread`, review on
  `points_somewhere`, drop on an exclusion, drop on `otherwise`, the margin
  overriding a respond path, and a feature sitting on its threshold that the path
  never consulted and must ignore.

Each case carries an `expected` block produced by running `route()` at the
pinned revision over the answers beside it, so the fixture records observed
behaviour rather than a restatement of this document.

The seven route tests at the pinned revision port across unchanged in substance,
because they already use invented exclusion names and already exercise respond,
review, drop, exclusion precedence, the `answerable and about` conjunction, and
the margin override including the unconsulted-feature case.

## Configuration

| variable | default | required under |
|---|---|---|
| `RELEVANCE_CLASSIFIER` | `llm` | always (`llm` or `jev`) |
| `TYPESAFE_API_KEY` | none | `jev` |
| `TYPESAFE_BASE_URL` | `https://api.typesafe.ai` | never |
| `TYPESAFE_CATALOGUE_PATH` | shadow fixture | `jev`, explicitly |
| `KEYWORD_PREFILTER` | `true` | must be `true` under `jev` |
| `RELEVANCE_HOLDOUT_RATE` | `0.1` | never |

Catalogue, credential, endpoint and routed-input requirements are validated only
when the classifier is `jev`, so an `llm` deployment needs none of them and
rollback stays one variable.

`TYPESAFE_CATALOGUE_PATH` is shared with the disabled shadow node, whose default
points at a shadow-form fixture the JEV loader would reject. Under `jev` the
variable must be set explicitly; validation fails rather than falling back to
that default.

## Deploying

The catalogue is mounted read-only into the container from a run-receipts
checkout on the host, and the classifier is selected in the app's environment
file. Rollback is `RELEVANCE_CLASSIFIER=llm`. The springfield repository holds the
compose and deploy specifics; they are not restated here, because that file is
what actually runs.
