# Typesafe relevance primary report

Run date: 2026-09-18

Assay base commit: `3d3437eb4be2b71bbfdf138763824d9518aee951`

Scout population commit: `17cf35fb7a7da23cb9aebdbcd81b13983fed2cfd`

## Frozen inputs

- Population: 79 distinct frozen evaluations: agent-ops snapshot `cb09c4f0` (51)
  plus agent-evals snapshot `33bf631e` excluding the 45 `GAIA`-route rows (28).
- Population SHA-256: `6c3d18de02e35f33a432be9f9bda57d36d22478a69fad94c42d924d4ccc9782a`.
- Selected/tested catalogue: `src/assay/investigations/relevance/catalogues/agent-ops-relevance.v1.yaml`.
- Catalogue version (SHA-256 of canonical JSON):
  `019c2b2ad3872710e0324d190a918bdfd5030612b013a8d6885186d261366e46`.
- Catalogue file SHA-256: `27c54ff7daf18383fcee7c947ef11290b81a6e24bfc55c3e1779c1bea590b88e`.
- Candidate: Typesafe `jev-latest`, resolved by the service to `jev-1.13.0`, using
  `typesafe-sdk==0.6.0`, three repeats, SDK retries disabled.
- Decision mapping: `agent_ops_relevance/v1` (`decide_argmax`), majority over the
  three candidate decisions.
- Reference: the production score and decision frozen with each Scout evaluation.

Raw post text stayed in the private temporary population export and is not committed.
`exported-answers.json` contains evaluation identifiers, labels, production outputs,
Typesafe answer vectors, deterministic decisions, model names, usage, latency and
request identifiers; it contains no post state or text.

## Protocol note

The proposed experiment specified rerunning the production arm three times. This
report instead uses each evaluation's frozen recorded production decision as the
reference. The deviation is conservative about historical fidelity but means this
report must not be represented as evidence about production repeat variance.

## Primary result

| Arm | Accuracy | Positive precision |
| --- | ---: | ---: |
| Frozen production reference | 65/79 (82.28%) | 47/59 (79.66%) |
| Typesafe primary, majority of 3 | 49/79 (62.03%) | 29/39 (74.36%) |

Paired correctness had 24 reference-only-correct cases and 8 Typesafe-only-correct
cases. The two-sided exact McNemar p-value is `0.0070003666914999485`.

## Verdict

**Typesafe did not win.** It was neither at least as accurate as production nor no
worse in positive-class precision, so the pre-registered adoption rule fails. The
Typesafe SDK backend must not be built from this report. The studied catalogue and
pure reference mappings remain the adoption artifact for backend-independent shadow
and reproduction tests.
