# Comparison data

Start with the [results and recommendation](../RESULTS.md).

| File | Contents |
|---|---|
| [comparison.json](comparison.json) | Model metrics, paired intervals, costs, verification counts and all 40 cases’ predictions. No post text. |
| [provenance.json](provenance.json) | Input/code/plan identities, run IDs, cost caveat, deployment checks, backup locations and audit-file hashes. |

The full canonical reports, logs, snapshots and exact campaign scripts are preserved in the [immutable audit archive in Git history](https://github.com/RankOneLabs/scout/tree/fcb89c68a0aa1e6a33ec0ac988f36f1ddca56833/evidence/heldout-agent-ops-2026-09-11/results) and the verified archive on willie identified in `provenance.json`. Private post text and databases remain outside this repository.
