# Relevance holdouts and the JEV handoff

Scout gains a second relevance classifier (JEV, answered by Typesafe System One)
and durable evidence for the decision it produces, so a sampled holdout can be
exported for blind grading and released later against stored labels.

This document is the contract record for that work. The project owner confirmed
the four product choices below on 2026-09-24. The packet, labels, and private
answer key use the existing assay and run-receipts formats described below.
Release orchestration and merging remain gated on the private catalogue parity
check; its inputs are not committed here.

## Confirmation status

**Confirmed by the project owner on 2026-09-24:**

1. JEV requires keyword prefiltering. An unexpected unrouted post fails
   retryably before the HTTP request, while LLM routing keeps its current behavior.
2. JEV stores compatibility scores of 1.0 for `respond` and `review`, and 0.0
   for `drop`. These values are not calibrated confidence.
3. LLM stores `respond` only when relevant and at or above the threshold in
   force at decision time; otherwise it stores `drop`. It never stores `review`.
4. Holdout export extends the population record with stable holdout,
   evaluation, post, and frozen project identity; a private envelope isolates
   the production decision. Labels join through the private key's case mapping,
   never row order or URL alone.

The existing assay packet builder and the round 5 artifacts in run-receipts
define the interchange. Scout's `holdout-export.v1` is the population input to
that builder. `assay-packet.v1`, `assay-labels.v3`, and `assay-key.v2` schemas
record the three existing artifact shapes. The prior Scout-only label sketch
was removed. No private catalogue, packet content, label content, or answer
key is committed here.

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
targets. Completion and failure also require an unexpired claim lease, even
before another holder takes over; token and fence must still match. Takeover
advances the fence.

## The interchange

The versioned Scout population export under `contracts/relevance/` is
`holdout-export.v1.schema.json`. Sampling, export, packet building and release
orchestration belong to C02. The three assay schemas in the same directory
describe the packet, labels, and key C02 exchanges with assay.

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

Assay's round 5 artifacts in run-receipts establish the actual three-file
contract. The blind `packet.json` is `assay.label-packet/v1`, with `name`,
`sitting`, `digest`, `plan_digest`, `rubric`, and blind `cases` containing only
`case_id`, `text`, and `parent_text`. The saved `labels.json` is
`assay.label-packet-labels/v3`, with `packet`, `packet_digest`, `plan_digest`,
`reviewer`, `saved_at`, and cases with `case_id`, `exclusion`, `needs_thread`,
`substance`, and `note`. The private `answer-key-private.json` is
`assay.label-packet-key/v2`; its case mapping contains `case_id`,
`evaluation_id`, `project_key`, `production_decision`, and `production_score`.
These names and versions are observable in the existing artifacts, not new
Scout inventions.

C02 must verify the packet and plan digests across all three files, resolve
each label's `case_id` through the private key, then verify that the mapped
`evaluation_id` and `project_key` match the held evaluation and frozen project.
It must reject unknown or duplicate case IDs. The private key never enters the
blind packet. C01 stores structured label and key provenance on completion;
it does not sample, build packets, or orchestrate release.

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

Every successful recorded decision gets a generated stable unique
`decision_uid` and an explicit `selected_for_holdout` flag. C02's sampling
step supplies the selection; C01 persists it with the evaluation and any hold.
The decision, evaluation, hold, and phase contributor links share the same
transaction so a failed write cannot leave a partial outcome.

Historical rows stay null. No stored fact identifies which classifier produced a
pre-JEV evaluation, so nothing is backfilled and nothing is guessed. A null there
means unknown, and reads as unknown.

## Fixtures

Neither redistributable catalogue at the pinned assay revision is in the form
`route()` consumes: `band-form.yaml` is the band-era catalogue feeding
`mappings.decide_argmax`, and `label-form.yaml` is the human label form. Scout
uses an invented response fixture at `tests/fixtures/relevance/`. Assay's
`tests/test_relevance_packet.py` supplies synthetic packet cases for the
interchange; private round 5 artifacts are used only to identify the format,
never copied into Scout:

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

## Status views

Three facts about one evaluation are recorded separately and are rendered
separately. Collapsing any two of them reports something nobody decided.

| fact | where it lives | what it means |
|---|---|---|
| source action | `relevance_decisions.action` | what the classifier decided |
| release authority | `relevance_holdouts.release_authority` / `release_action` | what acted on the hold: a blind label, or the recorded action |
| actual outcome | `evaluations.surface_status` | what actually happened |

A released `respond` that the critic then rejected is a real and ordinary
combination: authority `recorded_action`, action `respond`, and a target whose
`surface_status` is `critic_rejected`. The UI shows all three.

`held` is neither `surfaced` nor `drafting_failed`. A hold reached a decision
and stopped before drafting, so nothing about it failed and nothing about it
was published. No held row is actionable for posting, and none appears in a
draft or review queue — a held evaluation has no draft to queue. It stays
visible in the scan's evaluation view, with its own count and its own filter
value.

A released target records no classifier of its own. Its authority is the
hold's release record, not a classifier run, and a null classifier there
reads as unknown rather than being backfilled with a guess. The same is true
of every pre-JEV row.

### The repository-side audit

```bash
uv run scout analysis status-audit --db-path scout.db
```

Reads the status counts every view reads, plus the lifecycle invariants those
views depend on: a held row with a draft or a surfaced event, a hold whose
source is not held, a release that landed on a held evaluation, a decision
sampled into the holdout with no hold recorded, a released hold with no
recorded authority. It reports counts and invariant names only — never post
content, answers, catalogue identity or a label — so its output can be
attached to a deployment record. A database predating the holdout schema
audits clean with empty classifier and holdout counts.

A non-empty `findings` array is a stop: it means the database disagrees with
what the lifecycle guarantees, and no further rollout step should run until
it is understood.

## Rolling out

### Rollback

Rollback is one variable: `RELEVANCE_CLASSIFIER=llm`. Every JEV requirement —
the credential, the catalogue path, the `KEYWORD_PREFILTER=true` requirement —
is validated only under `jev`, so a rolled-back deployment cannot fail startup
on a setting it no longer uses.

`RELEVANCE_HOLDOUT_RATE` is a separate control and is read whatever the
classifier is. Rolling back neither resets it nor disables sampling; set it
to `0` if sampling should also stop.

Pending holds survive the rollback. A hold is a recorded decision and a
recorded action, not a live classifier session: `scout holdout release` acts
on the action stored at decision time, so a hold taken under `jev` still
releases as the `jev` decision said after the classifier has gone back to
`llm`. Release never recomputes a threshold and never re-classifies. A
rollback therefore loses no pending decision, and the pending population can
be exported and released at any point afterwards.

### External gates

Repository implementation is not rollout completion. Each gate below is
outside this repository, and none of them is satisfied here. The evidence
owner is where the evidence is produced and where it has to be recorded.

**Every gate in the table is open, and stays open until its named owner
supplies the evidence.** A gate closes on an artifact from that owner — a
recorded parity run on otto, a scored packet from assay, a deployed compose
file and a secret check on willie, a checked-out run-receipts on the host, a
canary scan with its audit output and its exercised rollback. Nothing in this
repository can close one of them.

In particular, repository checks are not evidence for any gate. A green CI
run, a passing test suite, a clean `ruff`/`mypy`, and the integrated scan
tests in `tests/test_holdout_lifecycle.py` all say the same thing: the code in
this repository behaves as its contract says. None of them observes the real
v6 catalogue, the round 5 records, the willie container, the mounted secret or
a live scan, because none of those is present here — the catalogue and the
graded records are private and deliberately uncommitted. Reading a repository
check as gate evidence would be reading a test of the port as a test of the
thing the port talks to.

| gate | what must be shown | evidence owner / source |
|---|---|---|
| real-v6 parity | On otto, with the real v6 catalogue: Scout sends the same `questions` mapping assay's loader produces, and projects a round 5 export record to the same state as assay's `build_state`. Any question or state mismatch blocks JEV enablement. | otto, against the private catalogue in run-receipts and assay's loader/`build_state` at the pinned revision |
| assay blind and score conformance | One blind packet per pending export record; rounds 4/5 form preserved; the final action and the respond/review-versus-drop split scored against the recorded classifier actions, with actual `surface_status` kept separate from the scored intent. | assay, in separately provisioned work |
| catalogue mount and secret | The v6 catalogue mounted read-only into the container on willie from a run-receipts checkout, `TYPESAFE_CATALOGUE_PATH` pointing at the mounted path, and `TYPESAFE_API_KEY` present from the environment and absent from every log and stored row. | springfield, `machines/willie/docker-compose.yml`, deployed through `machines/willie/scripts/deploy-scout.sh` |
| run-receipts availability | run-receipts cloned on the host with forwarded SSH, holding the v6 catalogue and the round 5 artifacts the parity check reads. | run-receipts, on willie |
| canary | A live scan under `RELEVANCE_CLASSIFIER=jev` with a clean `scout analysis status-audit`, and a recorded, exercised `RELEVANCE_CLASSIFIER=llm` rollback. | the production deployment |

None of these were executed in this repository and none is represented as
completed. Scout's own test suite asserts the port's behaviour and the shapes
the port must satisfy; it asserts no parity with the graded runs, because the
catalogue and the round 5 records that parity needs are private.

### Enablement

`RELEVANCE_CLASSIFIER=jev` goes to production only when every gate above has
produced its evidence: real-v6 parity on otto, assay blind and score
conformance, read-only mount and secret validation on willie, a successful
canary, and a documented `llm` rollback. A mismatch in the real-v6 question
mapping or round 5 state blocks enablement on its own.

Enablement is not a repository decision. Merging this work changes the default
for nothing: `RELEVANCE_CLASSIFIER` defaults to `llm`, and the switch is an
environment change on the host made by whoever holds the closed gates'
evidence. Do not set `jev` on the strength of the checks in this repository.

## Deploying

The catalogue is mounted read-only into the container from a run-receipts
checkout on the host, and the classifier is selected in the app's environment
file. Rollback is `RELEVANCE_CLASSIFIER=llm`. The springfield repository holds the
compose and deploy specifics; they are not restated here, because that file is
what actually runs.
