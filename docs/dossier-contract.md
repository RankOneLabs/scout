# The dossier-source dossier contract

Scout is the **consumer** of the dossier data contract published alongside
the external `dossier-source` knowledge base.
The upstream repository authors and publishes the contract; Scout only reads it,
pinned to an immutable git revision. This document describes Scout's side
of that contract — what it validates, in what order, and how it stays
byte-identical to the producer's own TypeScript implementation. For the
authoritative source of the contract itself (schema authoring, the regex
grammar's canonical definition, and the Unicode normalization data
generation process), see the producer repository's `docs/dossier-contract.md`,
`docs/regex-grammar.md`, and `docs/unicode-normalization.md`.

## Module layout

`scout.dossiers.contract` is the stable facade for this contract. It
implements `validate_dossier_semantics` directly and re-exports three
independently testable sub-concerns:

- `dossiers/normalization.py` — `canonical_normalize`,
  `normalize_subject_for_regex` (Unicode normalization).
- `dossiers/regex.py` — `PortableRegexError`, `parse_portable_regex`,
  `ascii_fold`, `compile_portable_regex` (the portable regex grammar and
  its compiler to native `re` patterns).
- `dossiers/prohibitions.py` — `ProhibitionMode`, `prohibition_matches`
  (dispatches a single prohibition's matcher, built on the two modules
  above).

Each has its own focused test file (`tests/test_dossier_normalization.py`,
`tests/test_portable_regex.py`, `tests/test_prohibition_matching.py`) for
the Python-engine-specific edge cases that aren't covered by the shared
conformance corpus. `tests/test_dossier_contract.py` keeps direct unit
coverage of `validate_dossier_semantics`'s rule-by-rule behavior, since
that function stays implemented in the facade itself.

## One revision selects data, schema, and corpus together

A dossier-source commit SHA is not just a data pointer. `resolve_dossier`
(`scout.dossiers.resolver`) retrieves `schemas/index.v1.schema.json` and
`schemas/summary.v1.schema.json` via `git show` from the *same requested
revision* as the `index.yaml`/summary YAML it validates. Changing the
pinned revision therefore changes the validation contract, not just the
data — there is no way to read data at one revision while validating
against a schema from another.

`contracts/dossier-source-v1.json` is Scout's sole dossier-source contract pin:
`repository`, `revision`, `corpus_path`, `summary_schema_path`, and
`index_schema_path`. CI, the conformance test suite, and this document all
read the pin from that one file — nothing else carries an independent copy
of the revision or these paths.

Scout never vendors a copy of either schema, the conformance corpus, or any
alternate schema-shaped file:

- `scout.dossiers.resolver` reads schemas exclusively via `git show` at the requested
  revision — no local file, no bundled package copy, no network fetch.
- `tests/fixtures/dossier_source` — small, hermetic, generalized data used for
  readiness/projection unit tests — carries YAML data and `PROVENANCE` only,
  never a schema or corpus file.
- `tests/test_dossier_conformance.py` runs the real shared conformance
  corpus directly from a pinned dossier-source checkout (never a copy) —
  see "Running the shared conformance corpus" below.
- `tests/test_dossier_no_legacy_mirror.py` is a standing regression test
  asserting no tracked Scout file ever reintroduces a schema or corpus
  mirror (basenames and `contracts/dossier-source-v1.json`'s `corpus_path`
  directory, e.g. `conformance/`).

## Validation order

For each `resolve_dossier` call:

1. **Boundary checks**: the revision is a full 40-character lowercase SHA,
   the repository checkout is clean, and the revision resolves to a real
   commit.
2. **Schema retrieval and self-validation**: both pinned schemas are parsed
   as JSON, validated as well-formed JSON Schema Draft 2020-12 documents
   (`Draft202012Validator.check_schema`), and required to declare a
   versioned `$id` ending in the pinned schema path
   (`.../schemas/index.v1.schema.json` / `.../schemas/summary.v1.schema.json`;
   the producer's host is not part of the contract). An offline `referencing.Registry`
   containing only these two schemas is built — schema validation can
   never resolve a remote `$ref`.
3. **Index schema validation**, then entry selection and path-safety
   checks (relative, canonical, no `..` traversal).
4. **Summary schema validation** of the selected document.
5. **Scout's consumer-side checks** (`scout.dossiers.resolver._build_resolution`, a pure
   function operating on the already-schema-validated document): summary
   id/type match the request, `project_key` matches, `last_reviewed` is
   within the caller's `max_age_days` window, and `min_entries` is met.
6. **The shared semantic contract**
   (`scout.dossiers.contract.validate_dossier_semantics`, called by
   `_build_resolution` immediately after step 5): every cross-record
   constraint dossier-source's conformance manifest declares —
   evidence/fact/resource/prohibition/known-gap ids are globally unique,
   `last_reviewed` is not in the future, every cross-reference
   (`evidence_ids`, `resource_ids`, `related_fact_ids`,
   `related_resource_ids`) resolves, `safe_phrasings` and
   `forbidden_phrasings` are unique after `canonical_normalize`,
   `canonical_url` is unique after normalization, and every prohibition's
   `regex` pattern parses under the portable grammar. This is the *sole*
   production implementation of these checks — `dossier.resolve_dossier`,
   `scripts/check_dossiers.py` (via `resolve_dossier`), and
   `tests/test_dossier_conformance.py` (the conformance corpus runner) all
   call it, rather than each keeping its own copy that could drift from
   the others or from the producer.

A schema failure and a semantic failure both raise the same
`DossierResolutionError` — callers see one fail-closed boundary rather
than a `jsonschema`/pydantic exception leaking through.

Schemas are never cached across revisions: every `resolve_dossier` call is
revision-keyed. There is no in-process singleton schema cache and no
fallback to a locally installed schema package.

## Unicode normalization

`dossier_normalization.canonical_normalize` (re-exported as
`scout.dossiers.contract.canonical_normalize`) implements the producer's fixed
normalization order:

1. Replace CRLF and bare CR with LF.
2. Apply Unicode NFKC.
3. Apply full Unicode 15.0.0 case folding (vendored in
   `dossiers/unicode_casefold.py`, generated from `CaseFolding.txt` statuses `C`
   and `F` only — simple-only `S` and Turkic `T` mappings are excluded).
4. Collapse every maximal run of Unicode 15.0.0 `White_Space` code points
   (vendored from `PropList.txt`) to a single U+0020 space.
5. Trim leading/trailing U+0020 spaces.

This is vendored rather than delegated to the interpreter's own
`str.casefold()`/whitespace regexes so behavior stays byte-identical
across Python versions whose bundled Unicode database may differ from the
contract's pinned 15.0.0. `dossier_normalization.normalize_subject_for_regex`
is a separate, narrower normalization used only for regex matching: CRLF,
bare CR, U+2028, and U+2029 all become LF, with no NFKC/casefold/whitespace
collapse.

## Portable regex grammar and flags

`portable_regex.parse_portable_regex` accepts only the allowlisted subset
both the producer (JavaScript `RegExp`) and Scout (Python `re`) can execute
identically: literals, escaped regex punctuation, `.`, character classes
with simple ranges, `^`/`$` anchors, capturing and non-capturing groups,
alternation, and greedy/lazy `*`/`+`/`?`/`{m}`/`{m,}`/`{m,n}` quantifiers.
Lookaround, backreferences, named groups, inline flags, shorthand classes
(`\d`/`\s`/`\w`/...), Unicode property escapes, and any other
engine-specific construct are rejected.

Only `i`, `m`, and `s` flags are ever authored, and **omitted flags mean
none** — Scout never forces `IGNORECASE`/`MULTILINE`/`DOTALL` the way it
used to. `m` and `s` pass straight through to `re.MULTILINE`/`re.DOTALL`.
Authored `i` is implemented as ASCII-only case folding (both the compiled
pattern's literals/classes and the subject are ASCII-folded; the native
`re.IGNORECASE` flag is never set), because JavaScript and Python disagree
on Unicode case-insensitive equivalence tables.

One JS/Python engine divergence the port surfaced and corrects: Python's
bare `$` matches at the end of the string *or* just before a trailing
newline, while JavaScript's non-multiline `$` matches only the absolute
end of the string. `portable_regex`'s compiler emits `\Z` instead of `$`
when the `m` flag is absent, preserving cross-engine parity — see
`test_end_anchor_without_multiline_is_strict_like_js` in
`tests/test_portable_regex.py`.

Prohibition matching (`prohibition_matching.prohibition_matches`, re-exported
as `scout.dossiers.contract.prohibition_matches`) dispatches all three matcher
types through one path: `exact_phrase` is a
case-sensitive literal substring of the original text, `normalized_phrase`
is a substring after `canonical_normalize` of both pattern and text, and
`regex` is a portable-regex search (not full match) against the
line-normalized original text.

## Running the shared conformance corpus

`tests/test_dossier_conformance.py` executes dossier-source's
`conformance/v1/manifest.json` — the same fixtures, normalization vectors,
and prohibition vectors the producer's own test suite runs — against Scout's
schema validation, `scout.dossiers.contract.validate_dossier_semantics`,
normalization, and regex/matcher implementation. It asserts the checkout's
`HEAD` matches the expected revision (see below) before running, so a
moving branch can never substitute for the pinned commit.

Checkout discovery, verification, and the required-mode contract are all
implemented once in `tests/conftest.py` and shared by
`test_dossier_conformance.py`, `test_eval_corpus.py::TestLinterScript`, and
`test_phase1_eval_runner.py::TestHermeticPhase1Sweep` (all three carry the
`dossier_source_contract` pytest marker).

**`DOSSIER_SOURCE_MODE`** selects how a checkout is located:

- `pinned` (default) — `DOSSIER_SOURCE_PINNED_CHECKOUT` env var, else a sibling
  checkout at `../dossier-source`. Its `HEAD` must equal
  `contracts/dossier-source-v1.json`'s `revision`.
- `candidate` — an explicit opt-in for testing dossier-source's own working
  tree (e.g. from dossier-source's side before its own PR merges): requires
  both `DOSSIER_SOURCE_CANDIDATE_CHECKOUT` and `DOSSIER_SOURCE_CANDIDATE_SHA`,
  and the checkout's actual `HEAD` must equal the supplied SHA. It is never
  a silent fallback when the pin is missing.

To run the corpus locally in pinned mode:

```bash
DOSSIER_SOURCE_PINNED_CHECKOUT=/path/to/dossier-source uv run pytest tests/test_dossier_conformance.py
```

Without the env var (and no `../dossier-source` sibling checkout),
the corpus tests skip with a clear message.

**`DOSSIER_SOURCE_CONFORMANCE_REQUIRED=1`** turns that convenience skip into a
hard failure: a missing or wrong-`HEAD` checkout, a missing or empty corpus
manifest, or missing schemas all fail the whole pytest session before any
test executes, and any later skip of a `dossier_source_contract`-marked test
is itself converted into a failure.

CI may omit the external dossier-source checkout, in which case the contract
tests skip. Run them explicitly on a developer or release checkout that has
the pinned repository available. Use required mode for that explicit contract
run so an absent checkout, wrong revision, or skipped contract test fails
closed.

## Changing the pinned revision

Bumping the pinned dossier-source revision changes Scout's validation
contract, not just its data. Before bumping:

1. Confirm the new revision's `schemas/*.v1.schema.json` and
   `conformance/v1/` are published together (the producer's CI enforces
   byte parity between its package schemas and dossier-source's published
   copies).
2. Update `contracts/dossier-source-v1.json`'s `revision` field — Scout's sole
   pin; the conformance test suite reads it from there.
3. Review and update the generalized `tests/fixtures/dossier_source` data without
   copying personal or deployment-specific provenance into the repository.
4. Run the full suite, including `tests/test_dossier_conformance.py`
   against a checkout pinned to the new revision, before merging.
