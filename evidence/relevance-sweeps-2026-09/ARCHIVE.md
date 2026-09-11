# Exploratory evidence, September 2026

The retained results, prompts, tables and charts are historical exploratory evidence.
Some runs used an in-container dependency/backport or reasoning patch that was not
captured by their recorded environment fingerprint. The legacy table scripts also
select runs by observed failures, read live baseline labels, and do not correctly
account for superseding retries or repeated cases. These files do not establish a
reproducible model ranking.

The scripts require `--historical-exploratory` to regenerate this archive. Preserve
their outputs when comparing historical observations; do not use them for new runs.

New campaigns use `scout feedback grid expand`, the generated runner and
`scout feedback grid report`. Reports read frozen score targets, choose the latest
attempt in each retry chain, charge every attempt, and weight successful repeats
equally within each case. The manifest indexes explicit run IDs from outcome files;
missing sweeps and failures remain visible. See [the experiment workflow](../../docs/experiment-workflow.md).
