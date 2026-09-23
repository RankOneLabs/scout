# Relevance label schema evidence, September 2026

This public bundle contains safe, stable-ID comparisons for the same 79 cases graded by one reviewer. It supports the study page without publishing post text, identities, links, or reviewer note text. The joining and note-flag derivation script, `derive-sitting-comparison.py`, lives in the private Run Receipts repository, not Scout. The Assay implementation is `experiments/typesafe_relevance/repeatability.py` at commit `360a28923ff40cd1f576ac6dd73e3202d9f23dc9`.

- `summary.json` gives every displayed figure as a numerator, denominator, definition, and evidence status. The original comparison joins the September 19 census to the September 20 substance sitting. The second grading round compares the September 20 labels with the September 22 repeat submission.
- `sitting-comparison.json` has 79 stable-ID rows of the September 19 exclusion, band, and disposition, plus the September 20 exclusion, substance, and derived route. Its definitions enumerate every allowed label.
- `grading-round-comparison.json` has 79 stable-ID rows of first and second round labels and note-derived booleans. An excluded case has null substance because that question was not asked. Its definitions specify denominators and the note rule.
- `interpretations.json` paraphrases eight manually checked cases. Case 63's likely reading correction came from a later clarification, separately from the submitted notes.
- `preregistration.json` is a byte-identical copy of the approved Assay preregistration. Its SHA-256 is `c253841f8b66097a5932505d5a44add2ba1cf3cfb410b741674e5f392393b9c3`.
- `checksums.json` hashes the other six bundle files and records Assay provenance. Unlike the flat path-to-hash map in `evidence/heldout-agent-ops-2026-09-11`, this bundle keeps hashes under `files` and provenance in named fields. It excludes itself to avoid a recursive hash.

## Deriving the original figures

All denominators refer to the 79 joined rows unless a filter is stated. Exact exclusion agreement compares category strings; binary agreement compares whether each category equals `none`. Of the 18 exact-category changes, 13 move from `none` to an excluded category, two move from excluded to `none`, and three move between excluded categories. Disposition agreement compares the older disposition with the new derived route. It is retired because the old disposition blended content and willingness to reply. The derived route is `drop` for an exclusion or substance `none`, `respond` for `in_post`, and `review` for `pointer`.

The 7/12 figure filters old `review` cases now routed to `respond` and counts those already banded `substantive`. The preregistered band crosswalk selects old `substantive` and `pointer` bands, counting a match when the new substance is `in_post` and `pointer`, respectively; null substance counts as disagreement. Its 37/51 result was inconclusive under the declared thresholds. The exploratory 37/42 figure applies the same match test only where both exclusions are `none`. The 36/61 rate compares old disposition with new route only among cases with identical exclusion labels. These are distinct filters even when numerators coincide.

## Deriving the repeat figures

The complete decision compares the exclusion category and, when not excluded, the substance value. Exact exclusion compares categories; binary exclusion compares excluded versus clear. Substance agreement uses only cases clear in both rounds, so its 51-case denominator differs from the original crosswalk's 51. Route agreement applies the mapping above to each round. Differences are complements of agreement over 79, except the seven substance changes, which require both rounds clear with different substance values. Four of 15 changed complete decisions retain the same route. Route totals count each route by round. Six of seven substance changes move toward `none`.

The 72/79 minimum was approved at `2026-09-21T21:39:27Z` before the repeat sitting submitted at `2026-09-22T01:56:39Z`. The observed 64/79 complete-decision agreement is marked `preregistered_below_floor`. The 70/79 and 71/79 sensitivity readings are exploratory; they are not replacements for the preregistered result.

## Note flags

A second-round note has `split_note: true` only when it explicitly gives a two-way numeric allocation between two distinct allowed substance labels, as percentages or fractions. The two normalized values must sum to one within 0.05. A malformed allocation is excluded and its stable ID appears in `malformed_split_notes`. `note_names_earlier_answer` is true only when a valid split names the first-round substance value. A mere note-presence flag says nothing about its explanation.

The note breakdown counts differing cases with second-round notes, substance differences with notes or valid splits naming the earlier answer, and exclusion changes with or without a second-round note. A route-changing exclusion difference without such a note counts as unexplained. Valid splits on agreeing cases count as held; all valid splits form the denominator for the held rate. The 70/79 sensitivity reading adds the six substance differences whose valid split names the earlier answer to 64 exact agreements; the 71/79 reading adds the one later, likely reading correction. Under this rule, 12 valid split notes and four held cases replace the provisional 13 and five; one malformed split is excluded.
