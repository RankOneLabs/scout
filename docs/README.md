# Scout documentation

Start with the root [README](../README.md) to install Scout and run a scan.
The documents here cover design, configuration, and operator procedures.

## Guides

- [Configuration](configuration.md) — environment variables, model routing,
  request volume, and cost considerations
- [Deployment security](deployment-security.md) — sidecar and web boundaries,
  trusted hosts, and credential rotation
- [Evaluations](evaluations.md) — the Phase 1 corpus, evidence audit, and model
  sweeps
- [Grading and feedback](grading-and-feedback.md) — corpus auditing,
  remediation, revision convergence, and feedback snapshots
- [Keyword routing](keyword-routing.md) — project routing and prompt overrides
- [Platform adapters](platform-adapters.md) — supported sources and the adapter
  contract

## Architecture and contracts

- [Architecture](architecture.md)
- [Dossier contract](dossier-contract.md)
- [Transactions and scan durability](transactions-and-scan-durability.md)
- [Finalized-grade Jig rebuild](finalized-grade-jig-rebuild.md)

## Operator procedures

- [PAA operations](runbooks/paa-operations.md)
- [Feedback policy activation](operations/feedback-policy-activation.md)
- [Grade revision convergence](operations/grade-revision-convergence.md)
- [Offline replay](operations/offline-replay.md)
