# Workflow validation pilot — September 11, 2026

Purpose: verify committed execution, recorded settings, durable attempt coverage,
failure propagation and canonical reporting end to end. These are exploratory
observations, not a held-out model-selection result.

- Population: the existing frozen agent-ops corpus (`cb09c4f0…`), all 51 cases.
  No label changes, route exclusions or case selection after observing outcomes.
- Prompt: the already committed agent-ops topical prompt, identical for both candidates.
- Candidates: hosted Qwen3 30B A3B Instruct 2507 and Gemma 4 26B A4B, reasoning
  explicitly disabled. One draw per candidate/case: 102 planned attempts.
- Runtime: a clean committed checkout and frozen dependencies. Record code commit,
  lockfile digest and Python version alongside expanded manifest and preview.
- Storage: independent copies of production Scout and trace snapshots, with a
  separate feedback database. Production experiment rows remain untouched.
- Budget: require preview estimate below $1 before executing. Report actual known
  provider cost and any unknown cost separately.
- Reporting: include both candidates and every attempted case. Canonical relevance
  scores use retained targets and equal case weight on the common successful
  population; report failures and coverage separately. No retries or run selection
  to improve the published result.
- Expansion criterion: both candidates complete the exact planned population and
  the generated outcome and report files agree with persisted state. A failure is
  evidence to inspect before scaling up.

Ollama on frink supplies feedback embeddings only; candidate inference uses
OpenRouter. Hosted provider routing/precision is not fixed, so this pilot does not
establish a controlled local-versus-hosted comparison.
